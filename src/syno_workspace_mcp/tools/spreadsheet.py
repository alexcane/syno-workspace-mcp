"""Synology Spreadsheet tools (15 tools, same names as upstream).

A spreadsheet is addressed by `file_id` (the Spreadsheet ID from the Office URL
`/oo/r/<id>`, or the URL itself) or by a `name` registered with `spreadsheet_register`.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from syno_workspace_mcp.clients.drive import DriveClient, normalize_drive_path
from syno_workspace_mcp.clients.spreadsheet import (
    BatchAction,
    CellValue,
    ExportFormat,
    SheetInfo,
    SpreadsheetClient,
    SpreadsheetInfo,
    block_range,
    column_name,
    scalar,
)
from syno_workspace_mcp.errors import InvalidInputError, SynologyError, to_tool_error
from syno_workspace_mcp.store import RegistryEntry, SpreadsheetRegistry, parse_spreadsheet_id
from syno_workspace_mcp.tools.common import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITE,
    Confirm,
    require_confirm,
)
from syno_workspace_mcp.tools.drive import DRIVE_CLIENT_KEY

SPREADSHEET_CLIENT_KEY = "spreadsheet"
REGISTRY_KEY = "spreadsheet_registry"

# Default read window when the sheet reports no size.
FALLBACK_RANGE = "A1:Z1000"

FileId = Annotated[
    str | None,
    Field(
        description="Spreadsheet ID from the Office URL /oo/r/<id> (or the URL itself), "
        "as shown by spreadsheet_list; not the Drive file ID"
    ),
]
Name = Annotated[
    str | None, Field(description="Name registered with spreadsheet_register (instead of file_id)")
]
RegistryPath = Annotated[
    str | None,
    Field(
        description="Drive path of the .osheet, e.g. '/team-folders/labs/x.osheet'. "
        "Alone it identifies the file; with name it picks between same-name registered files"
    ),
]
SheetName = Annotated[str, Field(min_length=1, description="Sheet (tab) name, e.g. 'Sheet1'")]
SheetId = Annotated[str, Field(description="Sheet ID, e.g. 'sh_1' (see spreadsheet_get_info)")]
Rows = Annotated[
    list[list[CellValue]],
    Field(
        min_length=1,
        description="2D array: outer list = rows, inner = cells, "
        "e.g. [['Name','Age'],['Alice',30]]. Strings starting with '=' are formulas.",
    ),
]


# Output shapes --------------------------------------------------------------


class SpreadsheetFile(BaseModel):
    file_id: str = Field(description="Drive file ID (not the Spreadsheet ID)")
    name: str
    path: str
    modified: str
    owner: str
    spreadsheet_id: str | None = Field(
        description="Spreadsheet ID to pass as file_id; null if Drive did not report it"
    )


class SpreadsheetFileList(BaseModel):
    total: int
    offset: int
    files: list[SpreadsheetFile]


class SheetData(BaseModel):
    file_id: str
    sheet_name: str
    range: str
    headers: list[str | None] = Field(description="First row of the range; null = empty cell")
    rows: list[list[CellValue]] = Field(description="Rows after the first one")
    total_rows: int
    total_cols: int


class StylesResult(BaseModel):
    sheet: str
    range: str
    styles: list[list[dict[str, Any]]]


class ExportResult(BaseModel):
    format: ExportFormat
    file_name: str
    mime_type: str
    size: int
    content_base64: str


class RegisterResult(BaseModel):
    success: bool
    spreadsheet_name: str = Field(description="Title reported by the Spreadsheet API")
    entry: RegistryEntry


class CreateResult(BaseModel):
    success: bool
    file_id: str
    name: str


class AddSheetResult(BaseModel):
    success: bool
    sheet_id: str
    index: int


class RenameSheetResult(BaseModel):
    success: bool
    sheet_id: str
    new_name: str


class WriteCellsResult(BaseModel):
    success: bool
    cells_written: int
    range_written: str


class AppendRowsResult(BaseModel):
    success: bool
    rows_appended: int
    range_written: str


class WriteStylesResult(BaseModel):
    success: bool
    sheet: str
    range_written: str
    rows_updated: int
    cols_updated: int


class BatchUpdateResult(BaseModel):
    success: bool
    action: BatchAction
    index: int
    count: int


class DeleteSheetResult(BaseModel):
    success: bool
    deleted_sheet_id: str


class DeleteFileResult(BaseModel):
    success: bool
    spreadsheet_id: str
    unregistered: bool = Field(description="True when a registry entry was removed")


# Style input (Spreadsheet API CellFormat) ------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NumberFormat(_Strict):
    pattern: str | None = Field(default=None, description="Number format pattern, e.g. '0.00%'")
    type: str | None = Field(
        default=None, description="Read-only in the API: accepted but has no effect"
    )


class TextFormat(_Strict):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    color: str | None = Field(default=None, description="Hex color without '#', e.g. 'ff0000'")
    name: str | None = Field(default=None, description="Font family, e.g. 'Arial'")
    size: int | None = Field(default=None, ge=1, le=409, description="Font size in points")


class CellFormat(_Strict):
    """Writing a format replaces the cell's whole previous format (API behaviour)."""

    numberFormat: NumberFormat | None = None
    verticalAlignment: Literal["top", "middle", "bottom"] | None = None
    horizontalAlignment: Literal["left", "center", "right"] | None = None
    textFormat: TextFormat | None = None
    bg: str | None = Field(default=None, description="Background hex color without '#'")
    wrapStrategy: Literal["wrap", "clip"] | None = None
    borders: list[str | None] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="Border colors [top, right, bottom, left]; null keeps a side, "
        "borders=null clears all",
    )


# Helpers --------------------------------------------------------------------


def _ss(ctx: Context) -> SpreadsheetClient:
    client = ctx.lifespan_context.get(SPREADSHEET_CLIENT_KEY)
    if not isinstance(client, SpreadsheetClient):  # pragma: no cover - wiring error
        raise RuntimeError("Spreadsheet client is not initialised")
    return client


def _registry(ctx: Context) -> SpreadsheetRegistry:
    registry = ctx.lifespan_context.get(REGISTRY_KEY)
    if not isinstance(registry, SpreadsheetRegistry):  # pragma: no cover - wiring error
        raise RuntimeError("Spreadsheet registry is not initialised")
    return registry


def _drive(ctx: Context) -> DriveClient:
    client = ctx.lifespan_context.get(DRIVE_CLIENT_KEY)
    if not isinstance(client, DriveClient):  # pragma: no cover - wiring error
        raise RuntimeError("Drive client is not initialised")
    return client


async def _resolve(ctx: Context, file_id: str | None, name: str | None, path: str | None) -> str:
    """Spreadsheet ID from `file_id`, a registered `name`, or a Drive `path` alone."""
    if file_id is not None and file_id.strip():
        return parse_spreadsheet_id(file_id)
    drive_path = normalize_drive_path(path) if path else None
    if name is not None and name.strip():
        return _registry(ctx).resolve(name.strip(), drive_path)
    if drive_path is not None:
        # Drive's `permanent_link` of an .osheet is its Spreadsheet ID.
        file, link = await _drive(ctx).get_with_link(drive_path)
        if file.extension != "osheet" or not link:
            raise InvalidInputError(f"'{file.path}' is not a Synology Spreadsheet (.osheet)")
        return parse_spreadsheet_id(link)
    raise InvalidInputError("one of file_id, name or path is required")


def _pick_sheet(info: SpreadsheetInfo, sheet_name: str | None) -> SheetInfo:
    if not info.sheets:
        raise InvalidInputError("this spreadsheet has no sheet")
    if sheet_name is None:
        return min(info.sheets, key=lambda sheet: sheet.index)
    for sheet in info.sheets:
        if sheet.name == sheet_name:
            return sheet
    names = ", ".join(f"'{sheet.name}'" for sheet in info.sheets)
    raise InvalidInputError(f"no sheet named '{sheet_name}'; sheets are {names}")


def _full_range(sheet: SheetInfo) -> str:
    if sheet.row_count < 1 or sheet.col_count < 1:
        return FALLBACK_RANGE
    return f"A1:{column_name(sheet.col_count - 1)}{sheet.row_count}"


def _trim(values: list[list[CellValue]]) -> list[list[CellValue]]:
    """Drop trailing empty rows and columns (sheets report their allocated size)."""

    def empty(cell: CellValue) -> bool:
        return cell is None or cell == ""

    rows = [list(row) for row in values]
    while rows and all(empty(cell) for cell in rows[-1]):
        rows.pop()
    width = max((i + 1 for row in rows for i, cell in enumerate(row) if not empty(cell)), default=0)
    return [row[:width] for row in rows]


def _cells(values: list[list[Any]]) -> int:
    return sum(len(row) for row in values)


def register_spreadsheet_tools(mcp: FastMCP) -> None:
    # Read ---------------------------------------------------------------------

    @mcp.tool(name="spreadsheet_list", annotations=READ_ONLY, tags={"spreadsheet"})
    async def spreadsheet_list(
        ctx: Context,
        folder_path: Annotated[
            str, Field(description="Drive folder, e.g. '/mydrive' or '/team-folders/NAME'")
        ] = "/mydrive",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> SpreadsheetFileList:
        """List Synology Spreadsheet (.osheet) files in a Drive folder with their Spreadsheet ID.

        Pass that `spreadsheet_id` as `file_id` to the other tools (or the file's `path`).
        """
        try:
            total, items = await _drive(ctx).list_with_links(
                folder_path, limit=limit, offset=offset, file_type="file", pattern="*.osheet"
            )
            registry = _registry(ctx)
            files = []
            for file, link in items:
                if file.extension != "osheet":
                    continue
                entry = None if link else registry.find_by_path(file.path)
                files.append(
                    SpreadsheetFile(
                        file_id=file.id,
                        name=file.name,
                        path=file.path,
                        modified=file.modified,
                        owner=file.owner,
                        spreadsheet_id=link or (entry.spreadsheet_id if entry else None),
                    )
                )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return SpreadsheetFileList(total=total, offset=offset, files=files)

    @mcp.tool(name="spreadsheet_get_info", annotations=READ_ONLY, tags={"spreadsheet"})
    async def spreadsheet_get_info(
        ctx: Context, file_id: FileId = None, name: Name = None, path: RegistryPath = None
    ) -> SpreadsheetInfo:
        """Get a spreadsheet's title and its sheets (ID, name, row and column counts)."""
        try:
            return await _ss(ctx).get_info(await _resolve(ctx, file_id, name, path))
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="spreadsheet_read_sheet", annotations=READ_ONLY, tags={"spreadsheet"})
    async def spreadsheet_read_sheet(
        ctx: Context,
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        sheet_name: Annotated[
            str | None, Field(description="Sheet name; omitted = first sheet")
        ] = None,
        range: Annotated[
            str | None,
            Field(description="A1 range, e.g. 'A1:D20'; omitted = the whole used area"),
        ] = None,
        include_formulas: Annotated[
            bool,
            Field(description="Return formulas as typed (e.g. '=B2*10') instead of results"),
        ] = False,
    ) -> SheetData:
        """Read cell values from a sheet: first row as headers, then the data rows.

        Dates come back as serial day numbers (1 = 1900-01-01), as stored by the sheet.
        """
        try:
            client = _ss(ctx)
            spreadsheet_id = await _resolve(ctx, file_id, name, path)
            if sheet_name is None or range is None:
                sheet = _pick_sheet(await client.get_info(spreadsheet_id), sheet_name)
                sheet_name, a1, trim = sheet.name, range or _full_range(sheet), range is None
            else:
                a1, trim = range, False
            # The values endpoint returns cells as entered: formulas come back as text.
            cells = await client.get_values(spreadsheet_id, sheet_name, a1)
            read_range, values = cells.range, cells.values
            if not include_formulas and _has_formula(values):
                values = await _computed(client, spreadsheet_id, sheet_name, a1, values)
        except SynologyError as error:
            raise to_tool_error(error) from error
        if trim:
            values = _trim(values)
            # Report the used area rather than the whole allocated grid that was read.
            width = max((len(row) for row in values), default=0)
            read_range = block_range("A1", len(values), width) if width else "A1"
        headers = [None if cell is None else str(cell) for cell in (values[0] if values else [])]
        rows = values[1:]
        return SheetData(
            file_id=spreadsheet_id,
            sheet_name=sheet_name,
            range=read_range or a1,
            headers=headers,
            rows=rows,
            total_rows=len(rows),
            total_cols=max((len(row) for row in values), default=0),
        )

    @mcp.tool(name="spreadsheet_get_styles", annotations=READ_ONLY, tags={"spreadsheet"})
    async def spreadsheet_get_styles(
        ctx: Context,
        sheet_name: SheetName,
        range: Annotated[str, Field(description="A1 range, e.g. 'A1:C10'")],
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
    ) -> StylesResult:
        """Get cell styles (fonts, colors, alignment, number formats, borders) for a range."""
        try:
            result = await _ss(ctx).get_styles(
                await _resolve(ctx, file_id, name, path), sheet_name, range
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return StylesResult(sheet=sheet_name, range=result.range or range, styles=result.styles)

    @mcp.tool(name="spreadsheet_export", annotations=READ_ONLY, tags={"spreadsheet"})
    async def spreadsheet_export(
        ctx: Context,
        format: Annotated[ExportFormat, Field(description="'xlsx' or 'csv'")],
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        sheet_name: Annotated[
            str | None, Field(description="CSV only: sheet to export; omitted = first sheet")
        ] = None,
    ) -> ExportResult:
        """Export a spreadsheet as XLSX, or one sheet as CSV, as base64 content (50 MB max)."""
        try:
            client = _ss(ctx)
            spreadsheet_id = await _resolve(ctx, file_id, name, path)
            sheet_id = None
            if format == "csv":
                sheet_id = _pick_sheet(await client.get_info(spreadsheet_id), sheet_name).sheet_id
            exported = await client.export(spreadsheet_id, format, sheet_id=sheet_id)
        except SynologyError as error:
            raise to_tool_error(error) from error
        return ExportResult(
            format=format,
            file_name=exported.file_name,
            mime_type=exported.mime_type,
            size=len(exported.content),
            content_base64=base64.b64encode(exported.content).decode("ascii"),
        )

    # Registry -----------------------------------------------------------------

    @mcp.tool(
        name="spreadsheet_register",
        annotations=WRITE.model_copy(update={"idempotent_hint": True}),
        tags={"spreadsheet"},
    )
    async def spreadsheet_register(
        ctx: Context,
        name: Annotated[str, Field(min_length=1, description="Name to use in the other tools")],
        spreadsheet_id: Annotated[
            str,
            Field(description="Spreadsheet ID from the Office URL /oo/r/<id>, or the URL itself"),
        ],
        path: Annotated[
            str | None,
            Field(description="Drive path of the file, to tell same-name files apart"),
        ] = None,
    ) -> RegisterResult:
        """Register a spreadsheet under a name so other tools can address it by name.

        The ID is checked against the Spreadsheet API before it is stored.
        """
        try:
            parsed_id = parse_spreadsheet_id(spreadsheet_id)
            drive_path = normalize_drive_path(path) if path else None
            info = await _ss(ctx).get_info(parsed_id)
            entry = _registry(ctx).register(name, parsed_id, path=drive_path, source="manual")
        except SynologyError as error:
            raise to_tool_error(error) from error
        return RegisterResult(success=True, spreadsheet_name=info.name, entry=entry)

    # Write --------------------------------------------------------------------

    @mcp.tool(name="spreadsheet_create", annotations=WRITE, tags={"spreadsheet"})
    async def spreadsheet_create(
        ctx: Context,
        name: Annotated[str, Field(min_length=1, description="Spreadsheet name, no extension")],
    ) -> CreateResult:
        """Create an empty spreadsheet and register it under its name.

        The Spreadsheet API cannot choose the folder: move the file with drive_move_file.
        """
        try:
            spreadsheet_id = await _ss(ctx).create(name)
            _registry(ctx).register(name, spreadsheet_id, path=None, source="create")
        except SynologyError as error:
            raise to_tool_error(error) from error
        return CreateResult(success=True, file_id=spreadsheet_id, name=name)

    @mcp.tool(name="spreadsheet_add_sheet", annotations=WRITE, tags={"spreadsheet"})
    async def spreadsheet_add_sheet(
        ctx: Context,
        sheet_name: SheetName,
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
    ) -> AddSheetResult:
        """Add a new sheet (tab) to a spreadsheet."""
        try:
            added = await _ss(ctx).add_sheet(await _resolve(ctx, file_id, name, path), sheet_name)
        except SynologyError as error:
            raise to_tool_error(error) from error
        return AddSheetResult(success=True, sheet_id=added.sheet_id, index=added.index)

    @mcp.tool(name="spreadsheet_rename_sheet", annotations=WRITE, tags={"spreadsheet"})
    async def spreadsheet_rename_sheet(
        ctx: Context,
        sheet_id: SheetId,
        new_name: Annotated[str, Field(min_length=1, description="New sheet name")],
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> RenameSheetResult:
        """Rename a sheet (tab). Formulas referring to the old name may break.

        Requires confirm=true.
        """
        require_confirm(confirm, f"rename sheet {sheet_id} to '{new_name}'")
        try:
            await _ss(ctx).rename_sheet(
                await _resolve(ctx, file_id, name, path), sheet_id, new_name
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return RenameSheetResult(success=True, sheet_id=sheet_id, new_name=new_name)

    @mcp.tool(name="spreadsheet_write_cells", annotations=DESTRUCTIVE, tags={"spreadsheet"})
    async def spreadsheet_write_cells(
        ctx: Context,
        sheet_name: SheetName,
        start_cell: Annotated[str, Field(description="Top-left cell, e.g. 'A1' or 'B3'")],
        values: Rows,
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> WriteCellsResult:
        """Write values to a block of cells, overwriting their content. Requires confirm=true."""
        require_confirm(
            confirm, f"write {len(values)} row(s) to '{sheet_name}' starting at {start_cell}"
        )
        try:
            written = await _ss(ctx).write_values(
                await _resolve(ctx, file_id, name, path), sheet_name, start_cell, values
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return WriteCellsResult(success=True, cells_written=_cells(values), range_written=written)

    @mcp.tool(name="spreadsheet_append_rows", annotations=WRITE, tags={"spreadsheet"})
    async def spreadsheet_append_rows(
        ctx: Context,
        sheet_name: SheetName,
        rows: Rows,
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> AppendRowsResult:
        """Append rows after the last row of the table that starts at A1. Requires confirm=true."""
        require_confirm(confirm, f"append {len(rows)} row(s) to '{sheet_name}'")
        try:
            result = await _ss(ctx).append_values(
                await _resolve(ctx, file_id, name, path), sheet_name, rows
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return AppendRowsResult(
            success=True, rows_appended=result.updated_rows, range_written=result.updated_range
        )

    @mcp.tool(name="spreadsheet_write_styles", annotations=DESTRUCTIVE, tags={"spreadsheet"})
    async def spreadsheet_write_styles(
        ctx: Context,
        sheet_name: SheetName,
        start_row: Annotated[int, Field(ge=0, description="0-based row offset (row 1 = 0)")],
        start_col: Annotated[int, Field(ge=0, description="0-based column offset (A = 0)")],
        styles: Annotated[
            list[list[CellFormat]],
            Field(
                min_length=1,
                description="Rectangular 2D grid; styles[i][j] applies to the cell "
                "(start_row + i, start_col + j)",
            ),
        ],
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> WriteStylesResult:
        """Overwrite the formats of a block of cells.

        Each given format replaces the cell's previous format entirely (unset
        attributes are cleared). Requires confirm=true.
        """
        width = len(styles[0])
        if width == 0 or any(len(row) != width for row in styles):
            raise ToolError("styles must be a rectangular grid (all rows the same length)")
        require_confirm(
            confirm, f"overwrite styles of {len(styles)}x{width} cells in '{sheet_name}'"
        )
        try:
            written = block_range(f"{column_name(start_col)}{start_row + 1}", len(styles), width)
            await _ss(ctx).write_styles(
                await _resolve(ctx, file_id, name, path),
                sheet_name,
                start_row,
                start_col,
                [[cell.model_dump(exclude_unset=True) for cell in line] for line in styles],
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return WriteStylesResult(
            success=True,
            sheet=sheet_name,
            range_written=written,
            rows_updated=len(styles),
            cols_updated=width,
        )

    @mcp.tool(name="spreadsheet_batch_update", annotations=DESTRUCTIVE, tags={"spreadsheet"})
    async def spreadsheet_batch_update(
        ctx: Context,
        sheet_id: SheetId,
        action: Annotated[BatchAction, Field(description="Insert or delete rows or columns")],
        index: Annotated[int, Field(ge=0, description="First row/column, 0-based (row 1 = 0)")],
        count: Annotated[int, Field(ge=1, le=10000, description="How many rows/columns")],
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> BatchUpdateResult:
        """Insert or delete rows or columns in a sheet. Requires confirm=true."""
        what, _, kind = action.partition("_")
        require_confirm(confirm, f"{what} {count} {kind} at index {index} in sheet {sheet_id}")
        try:
            await _ss(ctx).batch_update(
                await _resolve(ctx, file_id, name, path), sheet_id, action, index, count
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
        return BatchUpdateResult(success=True, action=action, index=index, count=count)

    @mcp.tool(name="spreadsheet_delete_sheet", annotations=DESTRUCTIVE, tags={"spreadsheet"})
    async def spreadsheet_delete_sheet(
        ctx: Context,
        sheet_id: SheetId,
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> DeleteSheetResult:
        """Delete a sheet (tab) and its content. Cannot be undone. Requires confirm=true."""
        require_confirm(confirm, f"permanently delete sheet {sheet_id}")
        try:
            await _ss(ctx).delete_sheet(await _resolve(ctx, file_id, name, path), sheet_id)
        except SynologyError as error:
            raise to_tool_error(error) from error
        return DeleteSheetResult(success=True, deleted_sheet_id=sheet_id)

    @mcp.tool(name="spreadsheet_delete_file", annotations=DESTRUCTIVE, tags={"spreadsheet"})
    async def spreadsheet_delete_file(
        ctx: Context,
        file_id: FileId = None,
        name: Name = None,
        path: RegistryPath = None,
        confirm: Confirm = False,
    ) -> DeleteFileResult:
        """Delete a whole spreadsheet file and remove it from the registry.

        Different from spreadsheet_delete_sheet, which removes one tab. Requires confirm=true.
        """
        require_confirm(confirm, f"delete the spreadsheet file {file_id or name}")
        try:
            spreadsheet_id = await _resolve(ctx, file_id, name, path)
            deleted = await _ss(ctx).delete(spreadsheet_id)
            unregistered = _registry(ctx).unregister(spreadsheet_id)
        except SynologyError as error:
            raise to_tool_error(error) from error
        return DeleteFileResult(
            success=True, spreadsheet_id=deleted or spreadsheet_id, unregistered=unregistered
        )


def _has_formula(values: list[list[CellValue]]) -> bool:
    return any(isinstance(cell, str) and cell.startswith("=") for row in values for cell in row)


async def _computed(
    client: SpreadsheetClient,
    spreadsheet_id: str,
    sheet_name: str,
    a1: str,
    values: list[list[CellValue]],
) -> list[list[CellValue]]:
    """Replace formula cells with their results (`effectiveValue` of the styles endpoint)."""
    start = a1.split(":", 1)[0]
    try:
        styles_range = block_range(start, len(values), max(len(row) for row in values))
    except InvalidInputError:
        styles_range = a1
    styles = await client.get_styles(spreadsheet_id, sheet_name, styles_range)
    computed = [list(row) for row in values]
    for i, row in enumerate(computed):
        for j, cell in enumerate(row):
            if not (isinstance(cell, str) and cell.startswith("=")):
                continue
            try:
                style = styles.styles[i][j]
            except IndexError:
                continue
            computed[i][j] = scalar(style.get("effectiveValue"))
    return computed
