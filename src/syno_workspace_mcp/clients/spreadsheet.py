"""Client of the `synology/spreadsheet-api` container (Spreadsheet API 3.4.1).

The container does not use the DSM session of `DsmClient`: `POST /spreadsheets/authorize`
makes it log in to DSM itself (at `dsm_url`) and returns a JWT, valid 28 days but tied to
that DSM session. The JWT embeds the DSM `sid` and `SynoToken`, so it is a secret.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal
from urllib.parse import quote, unquote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from syno_workspace_mcp.errors import InvalidInputError, SpreadsheetAuthError, SpreadsheetError
from syno_workspace_mcp.logging import forget_secret, register_secret

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
TRANSFER_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
MAX_EXPORT_BYTES = 50 * 1024 * 1024

CellValue = str | int | float | bool | None
Dimension = Literal["ROWS", "COLUMNS"]
BatchAction = Literal["insert_rows", "insert_columns", "delete_rows", "delete_columns"]
ExportFormat = Literal["xlsx", "csv"]

_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
_SHEET_ID = re.compile(r"sh_[0-9]+")
MAX_COLUMNS = 16384  # XFD


# A1 notation ----------------------------------------------------------------


def column_index(letters: str) -> int:
    """0-based index of a column name: A -> 0, Z -> 25, AA -> 26."""
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def column_name(index: int) -> str:
    """Column name of a 0-based index: 0 -> A, 26 -> AA."""
    if not 0 <= index < MAX_COLUMNS:
        raise InvalidInputError(f"column index {index} is out of range")
    name = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def parse_cell(cell: str) -> tuple[int, int]:
    """`B3` -> (column 1, row 3); rows stay 1-based as in A1 notation."""
    match = _CELL.fullmatch(cell.strip().upper())
    if not match:
        raise InvalidInputError(f"'{cell}' is not a cell in A1 notation, e.g. 'A1' or 'B3'")
    column = column_index(match[1])
    if column >= MAX_COLUMNS:
        raise InvalidInputError(f"'{cell}' is beyond the last column (XFD)")
    return column, int(match[2])


def block_range(start_cell: str, rows: int, columns: int) -> str:
    """A1 range covering `rows` x `columns` cells from `start_cell`."""
    column, row = parse_cell(start_cell)
    start = f"{column_name(column)}{row}"
    end = f"{column_name(column + max(columns, 1) - 1)}{row + max(rows, 1) - 1}"
    return start if start == end else f"{start}:{end}"


def qualified_range(sheet_name: str, a1_range: str) -> str:
    if "!" in a1_range:
        raise InvalidInputError("pass the sheet in sheet_name, not as 'Sheet!' in the range")
    if not sheet_name.strip():
        raise InvalidInputError("sheet_name must not be empty")
    return f"{sheet_name}!{a1_range.strip()}"


def validate_sheet_id(sheet_id: str) -> str:
    cleaned = sheet_id.strip()
    if not _SHEET_ID.fullmatch(cleaned):
        raise InvalidInputError(
            f"'{sheet_id}' is not a sheet ID: expected 'sh_<n>' (see spreadsheet_get_info)"
        )
    return cleaned


# Raw API shapes (tolerant) --------------------------------------------------


class _Raw(BaseModel):
    model_config = ConfigDict(extra="allow")


class _RawSheetProperties(_Raw):
    title: str = ""
    sheetId: str = ""
    index: int = 0
    hidden: bool = False


class _RawWorksheet(_Raw):
    properties: _RawSheetProperties = Field(default_factory=_RawSheetProperties)
    rowCount: int = 0
    colCount: int = 0


class _RawSpreadsheetProperties(_Raw):
    title: str = ""


class _RawSpreadsheet(_Raw):
    id: str = ""
    properties: _RawSpreadsheetProperties = Field(default_factory=_RawSpreadsheetProperties)
    sheets: list[_RawWorksheet] = Field(default_factory=list)


class _RawValues(_Raw):
    range: str = ""
    values: list[list[Any]] = Field(default_factory=list)


class _RawStyleRow(_Raw):
    values: list[dict[str, Any]] = Field(default_factory=list)


class _RawStyles(_Raw):
    range: str = ""
    rows: list[_RawStyleRow] = Field(default_factory=list)


class _RawAppendUpdates(_Raw):
    updateRange: str = ""
    updateRows: int = 0


class _RawAppend(_Raw):
    updates: _RawAppendUpdates = Field(default_factory=_RawAppendUpdates)


class _RawAddedSheet(_Raw):
    properties: _RawSheetProperties = Field(default_factory=_RawSheetProperties)


class _RawAddSheet(_Raw):
    addSheet: _RawAddedSheet = Field(default_factory=_RawAddedSheet)


class _RawSpreadsheetId(_Raw):
    spreadsheetId: str


# Output shapes ----------------------------------------------------------------


class SheetInfo(BaseModel):
    sheet_id: str = Field(description="Sheet ID, e.g. 'sh_1'")
    name: str
    index: int
    row_count: int
    col_count: int
    hidden: bool


class SpreadsheetInfo(BaseModel):
    file_id: str = Field(description="Spreadsheet ID")
    name: str
    sheets: list[SheetInfo]


class CellRange(BaseModel):
    range: str
    values: list[list[CellValue]]


class StyleRange(BaseModel):
    range: str
    styles: list[list[dict[str, Any]]]


class AppendResult(BaseModel):
    updated_range: str
    updated_rows: int


class AddedSheet(BaseModel):
    sheet_id: str
    name: str
    index: int


class ExportedFile(BaseModel):
    file_name: str
    mime_type: str
    content: bytes


def scalar(cell: Any) -> CellValue:
    """Flatten a CellJSON value: rich text becomes its text, formula errors their code."""
    if cell is None or isinstance(cell, str | int | float | bool):
        return cell
    if isinstance(cell, dict):
        if cell.get("t") == "r" and isinstance(cell.get("v"), list):
            return "".join(str(run.get("tx", "")) for run in cell["v"] if isinstance(run, dict))
        if isinstance(cell.get("error"), str):
            return str(cell["error"])
    return None


def _parse[T: BaseModel](model: type[T], payload: Any, what: str) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise SpreadsheetError(f"unexpected Spreadsheet API response for {what}") from error


def _authorize_host(dsm_url: str) -> tuple[str, str]:
    """`host` (with port unless default) and `protocol` fields for the authorize body."""
    url = httpx.URL(dsm_url)
    if url.scheme not in ("http", "https") or not url.host:
        raise InvalidInputError(f"invalid DSM URL for the Spreadsheet API: {dsm_url}")
    host = url.host if url.port is None else f"{url.host}:{url.port}"
    return host, url.scheme


# Client ---------------------------------------------------------------------


class SpreadsheetClient:
    """One HTTP client and one API token for the whole server.

    The token is obtained lazily. A 401 on a regular call means it expired or was
    revoked: the client authorizes again once and replays the request once (a
    rejected request was not executed, so replaying a write is safe).
    """

    def __init__(
        self,
        api_url: str,
        dsm_url: str,
        username: str,
        password: SecretStr,
        *,
        verify_tls: bool = True,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._dsm_host, self._dsm_protocol = _authorize_host(dsm_url)
        self._username = username
        self._password = password
        self._http = httpx.AsyncClient(
            base_url=self._api_url, verify=verify_tls, timeout=timeout, follow_redirects=False
        )
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> SpreadsheetClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Revoke the token (best effort, also logs the container out of DSM)."""
        try:
            await self.revoke()
        finally:
            await self._http.aclose()

    async def revoke(self) -> None:
        async with self._lock:
            token, self._token = self._token, None
        if token is None:
            return
        try:
            await self._http.post(
                "/spreadsheets/authorize/token/revoke",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            logger.warning("Spreadsheet API token revoke failed: %s", type(error).__name__)
        finally:
            forget_secret(token)

    # Spreadsheet --------------------------------------------------------------

    async def get_info(self, spreadsheet_id: str) -> SpreadsheetInfo:
        payload = await self._json("GET", _sheet_path(spreadsheet_id), what="get info")
        raw = _parse(_RawSpreadsheet, payload, "get info")
        return SpreadsheetInfo(
            file_id=raw.id or spreadsheet_id,
            name=raw.properties.title,
            sheets=[
                SheetInfo(
                    sheet_id=sheet.properties.sheetId,
                    name=sheet.properties.title,
                    index=sheet.properties.index,
                    row_count=sheet.rowCount,
                    col_count=sheet.colCount,
                    hidden=sheet.properties.hidden,
                )
                for sheet in raw.sheets
            ],
        )

    async def create(self, name: str) -> str:
        payload = await self._json(
            "POST", "/spreadsheets/create", body={"name": name}, what="create"
        )
        return _parse(_RawSpreadsheetId, payload, "create").spreadsheetId

    async def delete(self, spreadsheet_id: str) -> str:
        payload = await self._json(
            "POST", "/spreadsheets/delete", body={"spreadsheetId": spreadsheet_id}, what="delete"
        )
        return _parse(_RawSpreadsheetId, payload, "delete").spreadsheetId

    async def export(
        self, spreadsheet_id: str, file_format: ExportFormat, *, sheet_id: str | None = None
    ) -> ExportedFile:
        if file_format == "xlsx":
            path, params = f"{_sheet_path(spreadsheet_id)}/xlsx", None
        else:
            path = f"{_sheet_path(spreadsheet_id)}/sheet/csv"
            params = {"sheetId": validate_sheet_id(sheet_id or "sh_1")}
        return await self._binary(path, params, what=f"{file_format} export")

    async def batch_update(
        self, spreadsheet_id: str, sheet_id: str, action: BatchAction, index: int, count: int
    ) -> None:
        if index < 0 or count < 1:
            raise InvalidInputError("index must be >= 0 and count >= 1")
        dimension: Dimension = "ROWS" if action.endswith("rows") else "COLUMNS"
        dimension_range = {
            "sheetId": validate_sheet_id(sheet_id),
            "dimension": dimension,
            "startIndex": index,
            # Inclusive end: on the NAS, endIndex = startIndex + 1 inserted 2 rows.
            "endIndex": index + count - 1,
        }
        request: dict[str, Any] = (
            {"insertDimension": {"range": dimension_range, "inheritFromBefore": True}}
            if action.startswith("insert")
            else {"deleteDimension": {"range": dimension_range}}
        )
        await self._json(
            "POST",
            f"{_sheet_path(spreadsheet_id)}/batchUpdate",
            body={"requests": [request]},
            what="batch update",
        )

    # Sheets -------------------------------------------------------------------

    async def add_sheet(self, spreadsheet_id: str, sheet_name: str) -> AddedSheet:
        payload = await self._json(
            "POST",
            f"{_sheet_path(spreadsheet_id)}/sheet/add",
            body={"sheetName": sheet_name},
            what="add sheet",
        )
        props = _parse(_RawAddSheet, payload, "add sheet").addSheet.properties
        return AddedSheet(sheet_id=props.sheetId, name=props.title or sheet_name, index=props.index)

    async def rename_sheet(self, spreadsheet_id: str, sheet_id: str, new_name: str) -> None:
        await self._json(
            "POST",
            f"{_sheet_path(spreadsheet_id)}/sheet/rename",
            body={"sheetId": validate_sheet_id(sheet_id), "sheetName": new_name},
            what="rename sheet",
        )

    async def delete_sheet(self, spreadsheet_id: str, sheet_id: str) -> None:
        await self._json(
            "POST",
            f"{_sheet_path(spreadsheet_id)}/sheet/delete",
            body={"sheetId": validate_sheet_id(sheet_id)},
            what="delete sheet",
        )

    # Values and styles --------------------------------------------------------

    async def get_values(self, spreadsheet_id: str, sheet_name: str, a1_range: str) -> CellRange:
        path = f"{_sheet_path(spreadsheet_id)}/values/{_seg(qualified_range(sheet_name, a1_range))}"
        raw = _parse(_RawValues, await self._json("GET", path, what="read values"), "read values")
        return CellRange(
            range=raw.range, values=[[scalar(cell) for cell in row] for row in raw.values]
        )

    async def write_values(
        self, spreadsheet_id: str, sheet_name: str, start_cell: str, values: list[list[CellValue]]
    ) -> str:
        a1 = _block_for(start_cell, values)
        path = f"{_sheet_path(spreadsheet_id)}/values/{_seg(qualified_range(sheet_name, a1))}"
        await self._json("PUT", path, body={"values": values}, what="write values")
        return a1

    async def append_values(
        self, spreadsheet_id: str, sheet_name: str, values: list[list[CellValue]]
    ) -> AppendResult:
        a1 = _block_for("A1", values)
        path = (
            f"{_sheet_path(spreadsheet_id)}/values/{_seg(qualified_range(sheet_name, a1))}/append"
        )
        payload = await self._json("PUT", path, body={"values": values}, what="append rows")
        updates = _parse(_RawAppend, payload, "append rows").updates
        return AppendResult(updated_range=updates.updateRange, updated_rows=updates.updateRows)

    async def get_styles(self, spreadsheet_id: str, sheet_name: str, a1_range: str) -> StyleRange:
        path = f"{_sheet_path(spreadsheet_id)}/styles/{_seg(qualified_range(sheet_name, a1_range))}"
        raw = _parse(_RawStyles, await self._json("GET", path, what="read styles"), "read styles")
        return StyleRange(range=raw.range, styles=[row.values for row in raw.rows])

    async def write_styles(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        start_row: int,
        start_col: int,
        formats: list[list[dict[str, Any]]],
    ) -> None:
        if not sheet_name.strip():
            raise InvalidInputError("sheet_name must not be empty")
        body = {
            "sheetName": sheet_name,
            "startRow": start_row,
            "startCol": start_col,
            "rows": [{"values": [{"userEnteredFormat": cell} for cell in row]} for row in formats],
        }
        await self._json(
            "PUT", f"{_sheet_path(spreadsheet_id)}/styles", body=body, what="write styles"
        )

    # Token --------------------------------------------------------------------

    async def _get_token(self) -> str:
        if self._token is not None:
            return self._token
        async with self._lock:
            if self._token is None:
                self._token = await self._authorize()
            return self._token

    async def _renew_token(self, stale: str) -> str:
        async with self._lock:
            if self._token is None or self._token == stale:
                forget_secret(stale)
                self._token = await self._authorize()
            return self._token

    async def _authorize(self) -> str:
        body = {
            "username": self._username,
            "password": self._password.get_secret_value(),
            "host": self._dsm_host,
            "protocol": self._dsm_protocol,
        }
        try:
            response = await self._http.post("/spreadsheets/authorize", json=body)
        except httpx.TimeoutException as error:
            raise SpreadsheetError(
                f"the Spreadsheet API at {self._api_url} did not answer in time"
            ) from error
        except httpx.HTTPError as error:
            raise SpreadsheetError(_unreachable(self._api_url, error)) from error

        if response.status_code == 401:
            raise SpreadsheetAuthError(
                f"the Spreadsheet API could not log '{self._username}' in to DSM at "
                f"{self._dsm_protocol}://{self._dsm_host}. Most likely the container cannot "
                "reach DSM at that URL (network, firewall, TLS certificate it does not trust) "
                "or DSM auto-block banned it; otherwise the account lacks the Synology Office "
                "privilege, uses 2FA, or the password is wrong. Check SYNO_SS_DSM_URL.",
                status=401,
            )
        if response.status_code >= 400:
            raise SpreadsheetAuthError(
                f"Spreadsheet API authorization failed: {_error_detail(response)}",
                status=response.status_code,
            )
        token = _json_body(response, "authorize").get("token")
        if not isinstance(token, str) or not token:
            raise SpreadsheetAuthError("Spreadsheet API authorization returned no token")
        register_secret(token)
        logger.info("authorized on the Spreadsheet API as %s", self._username)
        return token

    # HTTP -------------------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        what: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        transfer: bool = False,
    ) -> httpx.Response:
        token = await self._get_token()
        for attempt in range(2):
            try:
                request = self._http.build_request(
                    method,
                    path,
                    json=body,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=TRANSFER_TIMEOUT if transfer else self._http.timeout,
                )
                response = await self._http.send(request)
            except httpx.TimeoutException as error:
                raise SpreadsheetError(
                    f"the Spreadsheet API did not answer in time ({what})"
                ) from error
            except httpx.HTTPError as error:
                raise SpreadsheetError(_unreachable(self._api_url, error)) from error
            if response.status_code != 401 or attempt == 1:
                break
            logger.info("Spreadsheet API token rejected, authorizing again")
            token = await self._renew_token(token)
        if response.status_code >= 400:
            raise SpreadsheetError(_status_message(response, what), status=response.status_code)
        return response

    async def _json(
        self, method: str, path: str, *, what: str, body: dict[str, Any] | None = None
    ) -> Any:
        response = await self._send(method, path, what=what, body=body)
        if not response.content:
            return {}
        return _json_body(response, what)

    async def _binary(self, path: str, params: dict[str, str] | None, *, what: str) -> ExportedFile:
        response = await self._send("GET", path, what=what, params=params, transfer=True)
        if len(response.content) > MAX_EXPORT_BYTES:
            raise SpreadsheetError(f"export is larger than the {MAX_EXPORT_BYTES} bytes limit")
        return ExportedFile(
            file_name=_filename(response.headers.get("content-disposition", "")),
            mime_type=response.headers.get("content-type", "application/octet-stream"),
            content=response.content,
        )


def _sheet_path(spreadsheet_id: str) -> str:
    return f"/spreadsheets/{_seg(spreadsheet_id)}"


def _seg(value: str) -> str:
    """Percent-encode one path segment (`Sheet 1!A1:B2` -> `Sheet%201%21A1%3AB2`)."""
    return quote(value, safe="")


def _block_for(start_cell: str, values: list[list[CellValue]]) -> str:
    if not values or not any(values):
        raise InvalidInputError("values must contain at least one non-empty row")
    return block_range(start_cell, len(values), max(len(row) for row in values))


def _json_body(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise SpreadsheetError(f"the Spreadsheet API returned non-JSON for {what}") from error
    if not isinstance(payload, dict):
        raise SpreadsheetError(f"unexpected Spreadsheet API response for {what}")
    return payload


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return f"{value} (HTTP {response.status_code})"
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return f"{value['message']} (HTTP {response.status_code})"
    return f"HTTP {response.status_code}"


_STATUS_HINTS = {
    400: "invalid argument: check the sheet name, sheet ID and range",
    401: "the Spreadsheet API keeps rejecting the token",
    403: "permission denied on this spreadsheet",
    404: "spreadsheet or sheet not found",
}


def _status_message(response: httpx.Response, what: str) -> str:
    hint = _STATUS_HINTS.get(response.status_code, "request failed")
    return f"Spreadsheet API {what}: {hint} [{_error_detail(response)}]"


def _unreachable(api_url: str, error: httpx.HTTPError) -> str:
    return (
        f"cannot reach the Spreadsheet API at {api_url} ({type(error).__name__}); "
        "is the synology-spreadsheet-api container running? (SYNO_SS_API_URL)"
    )


def _filename(disposition: str) -> str:
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
    return unquote(match[1]) if match else "export"
