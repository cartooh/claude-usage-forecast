"""Google Sheets access (gspread + service account) with retry on 429/5xx.

Only two things are ever written:
- the B cell next to the "現在の使用量(%)" label in the pace sheet
- rows appended to the log sheet (created with a header if missing)
"""
import logging
import random
import time

log = logging.getLogger(__name__)

LOG_HEADER = [
    "記録日時(JST)", "観測日時(JST)", "週リセット日時(JST)", "週使用量(%)",
    "週開始からの経過h", "平均ペース(%/h)", "直近24hペース(%/h)",
    "予測使用量・平均ペース法(%)", "予測使用量・直近ペース法(%)",
    "5h使用量(%)", "5hリセット日時(JST)", "データ鮮度(h)", "備考",
    "Fable週使用量(%)", "Fable観測日時(JST)",
    "週累計トークン・入出力+キャッシュ書込(M)", "週累計トークン・キャッシュ読込(M)", "週累計API換算($)",
    "1%あたりトークン(M・キャッシュ読込除く)", "1%あたりAPI換算($)",
    "Fable週累計API換算($)", "Fable 1%あたりAPI換算($)",
    "上限到達中の消費・API換算($)", "従量課金(Usage credits)表示",
]
MACHINE_HEADER = [
    "端末", "週リセット日時(JST)", "更新日時(JST)", "入出力+キャッシュ書込(M)", "キャッシュ読込(M)",
    "API換算($)", "Fable API換算($)", "上限到達中API換算($)", "単価未設定モデル",
]
HOURLY_HEADER = ["時刻(JST)", "端末", "API換算($)", "入出力+キャッシュ書込(M)", "キャッシュ読込(M)", "応答数",
                 "最も使ったセッション", "そのセッションのAPI換算($)"]
MACHINE_CHART_DAILY_COL = 10   # K: daily table; A..I hold the hourly table (time + up to 8 machines)
MACHINE_CHART_ANCHOR_COL = 20  # U: charts
ALERT_HEADER = ["検知日時(JST)", "端末", "種類", "対象", "内容", "キー"]
ALERT_READ_ROWS = 300  # newest rows read back for de-duplication
CHART_HEADER = [
    "観測日時", "週使用量(%)", "ちょうど使い切るペース(%)", "予測・平均ペース法(%)", "予測・直近ペース法(%)",
    "上限(%)", "Fable週使用量(%)", "1%あたりトークン(M・キャッシュ読込除く)", "1%あたりAPI換算($)",
]
# earlier layouts that are extended in place (their rows are kept)
LEGACY_HEADER_LENS = (13, 15)
# column index -> number format pattern for a freshly created log sheet
_LOG_FORMATS = {
    0: ("DATE_TIME", "yyyy/mm/dd(ddd) hh:mm:ss"),
    1: ("DATE_TIME", "yyyy/mm/dd(ddd) hh:mm:ss"),
    2: ("DATE_TIME", "yyyy/mm/dd(ddd) hh:mm"),
    3: ("NUMBER", "0.0"),
    4: ("NUMBER", "0.00"),
    5: ("NUMBER", "0.000"),
    6: ("NUMBER", "0.000"),
    7: ("NUMBER", "0.0"),
    8: ("NUMBER", "0.0"),
    9: ("NUMBER", "0.0"),
    10: ("DATE_TIME", "yyyy/mm/dd(ddd) hh:mm"),
    11: ("NUMBER", "0.00"),
    13: ("NUMBER", "0.0"),
    14: ("DATE_TIME", "yyyy/mm/dd(ddd) hh:mm"),
    15: ("NUMBER", "0.000"),
    16: ("NUMBER", "0.0"),
    17: ("NUMBER", "0.00"),
    18: ("NUMBER", "0.000"),
    19: ("NUMBER", "0.000"),
    20: ("NUMBER", "0.00"),
    21: ("NUMBER", "0.000"),
    22: ("NUMBER", "0.00"),
}


class SheetError(Exception):
    pass


def with_retry(fn, *args, attempts=6, base=1.0, max_sleep=60.0, sleep=time.sleep, **kwargs):
    """Call fn, retrying with exponential backoff on HTTP 429/5xx and connection errors."""
    import gspread
    import requests

    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code is None or not (code == 429 or 500 <= code < 600) or i == attempts - 1:
                raise
            reason = "HTTP %s" % code
            if code == 429:
                # The read quota is per minute and shared by every machine: wait for the next minute.
                delay = min(max_sleep, 20.0 * (i + 1)) + random.uniform(0, 10.0)
                log.warning("Sheets API %s, retry %d/%d in %.1fs", reason, i + 1, attempts - 1, delay)
                sleep(delay)
                continue
        except (requests.ConnectionError, requests.Timeout) as e:
            if i == attempts - 1:
                raise
            reason = type(e).__name__
        delay = min(max_sleep, base * (2 ** i)) + random.uniform(0, base)
        log.warning("Sheets API %s, retry %d/%d in %.1fs", reason, i + 1, attempts - 1, delay)
        sleep(delay)


def _find_row(col_a, predicate):
    for idx, value in enumerate(col_a, start=1):
        if predicate(str(value).strip()):
            return idx
    return None


class GSheetClient:
    def __init__(self, cfg):
        import gspread

        if not cfg.spreadsheet_id or not cfg.credentials_path:
            raise SheetError("SPREADSHEET_ID / GOOGLE_APPLICATION_CREDENTIALS are not set")
        self.cfg = cfg
        self._gspread = gspread
        gc = with_retry(gspread.service_account, filename=cfg.credentials_path)
        self.sh = with_retry(gc.open_by_key, cfg.spreadsheet_id)
        self._log_ws = None

    # ---- pace sheet -------------------------------------------------------
    def _pace_ws(self):
        return with_retry(self.sh.worksheet, self.cfg.pace_sheet)

    def pace_settings(self):
        """Return {'usage_row', 'reset_weekday', 'reset_hour', 'usage_cell_formula'}."""
        ws = self._pace_ws()
        vals = with_retry(ws.get, "A1:B200", value_render_option="UNFORMATTED_VALUE")
        formulas = with_retry(ws.get, "A1:B200", value_render_option="FORMULA")
        col_a = [r[0] if r else "" for r in vals]

        def b(row, rows=vals):
            if row is None or row > len(rows):
                return None
            r = rows[row - 1]
            return r[1] if len(r) > 1 else None

        usage_row = _find_row(col_a, lambda s: s == self.cfg.current_usage_label)
        wd_row = _find_row(col_a, lambda s: s.startswith(self.cfg.reset_weekday_label_prefix))
        hr_row = _find_row(col_a, lambda s: s == self.cfg.reset_hour_label)
        return {
            "usage_row": usage_row,
            "usage_cell_formula": b(usage_row, formulas),
            "reset_weekday": b(wd_row),
            "reset_hour": b(hr_row),
        }

    def write_current_usage(self, used, settings=None):
        settings = settings or self.pace_settings()
        row = settings["usage_row"]
        if row is None:
            raise SheetError("label %r not found in column A" % self.cfg.current_usage_label)
        current = settings.get("usage_cell_formula")
        if isinstance(current, str) and current.startswith("="):
            raise SheetError("B%d holds a formula; refusing to overwrite" % row)
        ws = self._pace_ws()
        with_retry(ws.update, range_name="B%d" % row, values=[[format_percent(used)]],
                   value_input_option="USER_ENTERED")
        return "B%d" % row

    def read_pace_outputs(self):
        """Formatted A:B of the pace sheet, for verification after a write."""
        ws = self._pace_ws()
        return with_retry(ws.get, "A1:B200")

    # ---- log sheet --------------------------------------------------------
    def _find_log_ws(self):
        if self._log_ws is not None:
            return self._log_ws
        try:
            self._log_ws = with_retry(self.sh.worksheet, self.cfg.log_sheet)
        except self._gspread.exceptions.WorksheetNotFound:
            return None
        return self._log_ws

    def read_log(self):
        """All data rows (after the header) as unformatted values, dates as serials."""
        ws = self._find_log_ws()
        if ws is None:
            return []
        rows = with_retry(ws.get, "A2:%s" % _col(len(LOG_HEADER)), value_render_option="UNFORMATTED_VALUE",
                          date_time_render_option="SERIAL_NUMBER")
        return [list(r) + [""] * (len(LOG_HEADER) - len(r)) for r in rows]

    def ensure_log_sheet(self):
        ws = self._find_log_ws()
        if ws is None:
            ws = with_retry(self.sh.add_worksheet, title=self.cfg.log_sheet, rows=1000,
                            cols=len(LOG_HEADER))
            self._log_ws = ws
            log.info("created sheet %s", self.cfg.log_sheet)
        header = with_retry(ws.row_values, 1)
        filled = len([c for c in header if str(c).strip()])
        if (filled in LEGACY_HEADER_LENS and header[:filled] == LOG_HEADER[:filled]
                and not any(str(c).strip() for c in header[filled:])):
            # extend an existing log with the newer columns, keeping its rows
            if ws.col_count < len(LOG_HEADER):
                with_retry(ws.add_cols, len(LOG_HEADER) - ws.col_count)
            with_retry(ws.update, range_name="%s1" % _col(filled + 1),
                       values=[LOG_HEADER[filled:]])
            self._format_log(ws)
            log.info("log sheet extended with columns %s", LOG_HEADER[filled:])
        elif header[: len(LOG_HEADER)] != LOG_HEADER:
            if any(str(c).strip() for c in header):
                raise SheetError("log sheet row 1 is not the expected header; not touching it")
            with_retry(ws.update, range_name="A1", values=[LOG_HEADER])
            self._format_log(ws)
        return ws

    def _format_log(self, ws):
        sid = ws.id
        reqs = [
            {"updateSheetProperties": {"properties": {"sheetId": sid,
                                                      "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": False}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
        ]
        for col, (typ, pattern) in _LOG_FORMATS.items():
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": col,
                          "endColumnIndex": col + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": typ, "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat"}})
        with_retry(self.sh.batch_update, {"requests": reqs})

    def append_log(self, row):
        ws = self.ensure_log_sheet()
        with_retry(ws.append_row, row, value_input_option="USER_ENTERED",
                   insert_data_option="OVERWRITE", table_range="A1")

    # ---- chart data sheet -------------------------------------------------
    def write_chart_data(self, rows):
        """Replace A2:I of the chart sheet. Returns False if the sheet does not exist."""
        try:
            ws = with_retry(self.sh.worksheet, self.cfg.chart_sheet)
        except self._gspread.exceptions.WorksheetNotFound:
            return False
        with_retry(ws.batch_clear, ["A2:I"])
        if rows:
            with_retry(ws.update, range_name="A2:I%d" % (len(rows) + 1), values=rows,
                       value_input_option="USER_ENTERED")
        return True

    # ---- per-machine token totals ---------------------------------------
    def _machines_ws(self, create):
        try:
            return with_retry(self.sh.worksheet, self.cfg.machines_sheet)
        except self._gspread.exceptions.WorksheetNotFound:
            if not create:
                return None
        ws = with_retry(self.sh.add_worksheet, title=self.cfg.machines_sheet, rows=500,
                        cols=len(MACHINE_HEADER))
        with_retry(ws.update, range_name="A1", values=[MACHINE_HEADER])
        with_retry(self.sh.batch_update, {"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 1,
                                      "endColumnIndex": 3},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME",
                                                                            "pattern": "yyyy/mm/dd(ddd) hh:mm"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
        ]})
        log.info("created sheet %s", self.cfg.machines_sheet)
        return ws

    def read_machines(self):
        """All rows of the per-machine sheet (unformatted, dates as serials)."""
        ws = self._machines_ws(create=False)
        if ws is None:
            return []
        rows = with_retry(ws.get, "A2:%s" % _col(len(MACHINE_HEADER)),
                          value_render_option="UNFORMATTED_VALUE", date_time_render_option="SERIAL_NUMBER")
        return [list(r) + [""] * (len(MACHINE_HEADER) - len(r)) for r in rows]

    def upsert_machine(self, row):
        """Update this machine's row for the week (row[0]=machine, row[1]=week reset text), or append."""
        from .timeutil import cell_to_ts
        ws = self._machines_ws(create=True)
        existing = self.read_machines()
        key_ts = cell_to_ts(row[1])
        for i, r in enumerate(existing, start=2):
            try:
                if str(r[0]) == str(row[0]) and cell_to_ts(r[1]) == key_ts:
                    with_retry(ws.update, range_name="A%d" % i, values=[row], value_input_option="USER_ENTERED")
                    return "updated"
            except (ValueError, TypeError):
                continue
        with_retry(ws.append_row, row, value_input_option="USER_ENTERED",
                   insert_data_option="OVERWRITE", table_range="A1")
        return "appended"

    def delete_machine_rows(self, machine):
        """Remove every row of `machine` from the per-machine sheet. Returns the count."""
        ws = self._machines_ws(create=False)
        if ws is None:
            return 0
        rows = self.read_machines()
        idx = [i for i, r in enumerate(rows, start=2) if str(r[0]) == str(machine)]
        reqs = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                               "startIndex": i - 1, "endIndex": i}}}
                for i in sorted(idx, reverse=True)]
        if reqs:
            with_retry(self.sh.batch_update, {"requests": reqs})
        return len(idx)

    # ---- creating missing sheets ----------------------------------------
    def _alerts_ws(self, create):
        try:
            return with_retry(self.sh.worksheet, self.cfg.alerts_sheet)
        except self._gspread.exceptions.WorksheetNotFound:
            if not create:
                return None
        ws = with_retry(self.sh.add_worksheet, title=self.cfg.alerts_sheet, rows=1000, cols=len(ALERT_HEADER))
        with_retry(ws.update, range_name="A1", values=[ALERT_HEADER])
        with_retry(self.sh.batch_update, {"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 0,
                                      "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME",
                                                                            "pattern": "yyyy/mm/dd(ddd) hh:mm"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
        ]})
        log.info("created sheet %s", self.cfg.alerts_sheet)
        return ws

    def read_alerts(self):
        """The newest alert rows (unformatted, dates as serials); [] if the sheet does not exist yet."""
        ws = self._alerts_ws(create=False)
        if ws is None:
            return []
        n = len(with_retry(ws.col_values, 1))  # last filled row
        if n < 2:
            return []
        first = max(2, n - ALERT_READ_ROWS + 1)
        rows = with_retry(ws.get, "A%d:%s%d" % (first, _col(len(ALERT_HEADER)), n),
                          value_render_option="UNFORMATTED_VALUE", date_time_render_option="SERIAL_NUMBER")
        return [list(r) + [""] * (len(ALERT_HEADER) - len(r)) for r in rows if r and str(r[0]).strip()]

    def append_alert(self, row):
        ws = self._alerts_ws(create=True)
        with_retry(ws.append_row, row, value_input_option="USER_ENTERED",
                   insert_data_option="OVERWRITE", table_range="A1")

    # ---- hourly use per machine ------------------------------------------
    def _simple_ws(self, title, header, create, rows=1000, date_cols=1, pattern="yyyy/mm/dd(ddd) hh:mm"):
        try:
            return with_retry(self.sh.worksheet, title)
        except self._gspread.exceptions.WorksheetNotFound:
            if not create:
                return None
        ws = with_retry(self.sh.add_worksheet, title=title, rows=rows, cols=max(len(header), 26))
        with_retry(ws.update, range_name="A1", values=[header])
        reqs = [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
        ]
        if date_cols:
            reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": date_cols},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat"}})
        with_retry(self.sh.batch_update, {"requests": reqs})
        log.info("created sheet %s", title)
        return ws

    def append_hourly(self, rows):
        ws = self._simple_ws(self.cfg.hourly_sheet, HOURLY_HEADER, create=True, rows=2000)
        with_retry(ws.append_rows, rows, value_input_option="USER_ENTERED",
                   insert_data_option="OVERWRITE", table_range="A1")

    def read_hourly(self):
        ws = self._simple_ws(self.cfg.hourly_sheet, HOURLY_HEADER, create=False)
        if ws is None:
            return []
        rows = with_retry(ws.get, "A2:%s" % _col(len(HOURLY_HEADER)), value_render_option="UNFORMATTED_VALUE",
                          date_time_render_option="SERIAL_NUMBER")
        return [r for r in rows if r]

    def write_machine_chart(self, header, hourly, daily):
        """Rewrite the pivot tables; (re)build the two charts when the machine columns change."""
        title = self.cfg.machine_chart_sheet
        ws = self._simple_ws(title, header, create=True, rows=400, date_cols=0)
        old = with_retry(ws.row_values, 1)
        n = len(header)
        dcol = MACHINE_CHART_DAILY_COL
        with_retry(ws.batch_clear, ["A1:I", "%s1:%s" % (_col(dcol + 1), _col(dcol + 9))])
        with_retry(ws.update, range_name="A1", values=[header] + hourly, value_input_option="USER_ENTERED")
        with_retry(ws.update, range_name="%s1" % _col(dcol + 1), values=[["日付"] + header[1:]] + daily,
                   value_input_option="USER_ENTERED")
        sid = ws.id
        meta = with_retry(self.sh.fetch_sheet_metadata, {"fields": "sheets(properties.sheetId,charts.chartId)"})
        charts = [c["chartId"] for s in meta.get("sheets", []) if s["properties"]["sheetId"] == sid
                  for c in s.get("charts", [])]
        if len(charts) == 2 and old[:n] == header and len([c for c in old if str(c).strip()]) == n:
            return False
        reqs = [{"deleteEmbeddedObject": {"objectId": c}} for c in charts]
        reqs += [
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME",
                                                                            "pattern": "mm/dd(ddd) hh:mm"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": dcol,
                                      "endColumnIndex": dcol + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE",
                                                                            "pattern": "mm/dd(ddd)"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
        ]

        def src(col, rows):
            return {"sourceRange": {"sources": [{"sheetId": sid, "startRowIndex": 0, "endRowIndex": rows + 1,
                                                 "startColumnIndex": col, "endColumnIndex": col + 1}]}}

        def chart(title_, ctype, c0, rows, row_at, xtitle):
            return {"addChart": {"chart": {
                "spec": {"title": title_, "basicChart": {
                    "chartType": ctype, "stackedType": "STACKED", "legendPosition": "BOTTOM_LEGEND",
                    "headerCount": 1, "axis": [{"position": "BOTTOM_AXIS", "title": xtitle},
                                               {"position": "LEFT_AXIS", "title": "API換算 $"}],
                    "domains": [{"domain": src(c0, rows)}],
                    "series": [{"series": src(c0 + i, rows), "targetAxis": "LEFT_AXIS"} for i in range(1, n)]}},
                "position": {"overlayPosition": {
                    "anchorCell": {"sheetId": sid, "rowIndex": row_at, "columnIndex": MACHINE_CHART_ANCHOR_COL},
                    "widthPixels": 900, "heightPixels": 380}}}}}
        reqs.append(chart("端末別 1時間ごとのAPI換算(直近7日)", "AREA", 0, len(hourly), 0, "時刻"))
        reqs.append(chart("端末別 1日ごとのAPI換算(直近30日)", "COLUMN", dcol, len(daily), 20, "日付"))
        with_retry(self.sh.batch_update, {"requests": reqs})
        log.info("rebuilt charts on %s for %s", title, header[1:])
        return True

    def sheet_titles(self):
        return [w.title for w in with_retry(self.sh.worksheets)]

    def ensure_pace_sheet(self, template_path):
        """Create the pace sheet from the bundled template if it does not exist."""
        import json
        if self.cfg.pace_sheet in self.sheet_titles():
            return False
        tpl = json.load(open(template_path, encoding="utf-8"))
        ws = with_retry(self.sh.add_worksheet, title=self.cfg.pace_sheet, rows=100, cols=10, index=0)
        rows = {}
        for c in tpl["cells"]:
            rows.setdefault(c["r"], {})[c["c"]] = {k: v for k, v in c.items() if k not in ("r", "c")}
        row_data = []
        for r in range(max(rows) + 1):
            cells = rows.get(r, {})
            row_data.append({"values": [cells.get(ci, {}) for ci in range(max(cells) + 1 if cells else 0)]})
        reqs = [{"updateCells": {"start": {"sheetId": ws.id, "rowIndex": 0, "columnIndex": 0},
                                 "rows": row_data, "fields": "userEnteredValue,userEnteredFormat"}}]
        for ci, w in enumerate(tpl.get("columnWidths") or []):
            if w:
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": ci, "endIndex": ci + 1},
                    "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        with_retry(self.sh.batch_update, {"requests": reqs})
        log.info("created sheet %s from template", self.cfg.pace_sheet)
        return True

    def ensure_chart_sheet(self):
        """Create the chart sheet (header + 3 charts) if it does not exist."""
        if self.cfg.chart_sheet in self.sheet_titles():
            return False
        ws = with_retry(self.sh.add_worksheet, title=self.cfg.chart_sheet, rows=5000, cols=20)
        with_retry(ws.update, range_name="A1", values=[CHART_HEADER])
        sid = ws.id

        def src(col):
            return {"sourceRange": {"sources": [{"sheetId": sid, "startRowIndex": 0, "endRowIndex": 5000,
                                                 "startColumnIndex": col, "endColumnIndex": col + 1}]}}

        def rgb(r, g, b):
            return {"rgbColor": {"red": r, "green": g, "blue": b}}

        def chart(title, series, row, axes, ctype="LINE", vmax=None):
            axis = [{"position": "BOTTOM_AXIS", "title": "観測日時"}] + [
                {"position": p, "title": t} for p, t in axes]
            if vmax is not None:
                axis[1]["viewWindowOptions"] = {"viewWindowMin": 0, "viewWindowMax": vmax,
                                                "viewWindowMode": "EXPLICIT"}
            ser = []
            for col, color, ax, dashed in series:
                x = {"series": src(col), "targetAxis": ax, "colorStyle": color}
                if ctype == "COMBO":
                    x["type"] = "LINE"
                if dashed:
                    x["lineStyle"] = {"type": "MEDIUM_DASHED", "width": 2}
                ser.append(x)
            return {"addChart": {"chart": {
                "spec": {"title": title, "basicChart": {
                    "chartType": ctype, "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
                    "interpolateNulls": False, "axis": axis,
                    "domains": [{"domain": src(0)}], "series": ser}},
                "position": {"overlayPosition": {
                    "anchorCell": {"sheetId": sid, "rowIndex": row, "columnIndex": 10},
                    "widthPixels": 760, "heightPixels": 360}}}}}

        L, R = "LEFT_AXIS", "RIGHT_AXIS"
        reqs = [
            {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME",
                                                                            "pattern": "mm/dd(ddd) hh:mm"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
            chart("週間使用量の推移(直近14日)",
                  [(1, rgb(.10, .45, .85), L, False), (2, rgb(.55, .55, .55), L, False),
                   (6, rgb(.85, .35, .6), L, False)], 0, [(L, "%")], vmax=100),
            chart("リセット時点の予測使用量(直近14日)",
                  [(3, rgb(.93, .49, .13), L, False), (4, rgb(.55, .30, .75), L, False),
                   (5, rgb(.85, .2, .2), L, True)], 19, [(L, "%")], vmax=200),
            chart("使用量1%あたりのトークン数とAPI換算額(週累計ベース)",
                  [(7, rgb(.10, .55, .45), L, False), (8, rgb(.85, .55, .10), R, False)], 38,
                  [(L, "Mトークン / 1%(キャッシュ読込除く)"), (R, "API換算 $ / 1%")], ctype="COMBO"),
        ]
        with_retry(self.sh.batch_update, {"requests": reqs})
        log.info("created sheet %s with charts", self.cfg.chart_sheet)
        return True


def _col(n):
    """1 -> 'A', 15 -> 'O'."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def format_percent(used):
    """60 -> '60%', 55.4 -> '55.4%' (parsed by Sheets as a percentage)."""
    return ("%.2f" % float(used)).rstrip("0").rstrip(".") + "%"
