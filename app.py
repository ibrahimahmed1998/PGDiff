from flask import Flask, request, jsonify, render_template_string
import psycopg2
import psycopg2.extras
import json

app = Flask(__name__)

# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def get_conn(dsn):
    return psycopg2.connect(dsn, connect_timeout=10)

def fetch_all(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()

# ─────────────────────────────────────────────
# Schema extractors
# ─────────────────────────────────────────────

def get_tables(conn):
    rows = fetch_all(conn, """
        SELECT t.table_name,
               c.column_name,
               c.ordinal_position,
               c.data_type,
               c.character_maximum_length,
               c.numeric_precision,
               c.numeric_scale,
               c.is_nullable,
               c.column_default,
               c.udt_name
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema
         AND c.table_name  = t.table_name
        WHERE t.table_schema = 'public'
          AND t.table_type   = 'BASE TABLE'
        ORDER BY t.table_name, c.ordinal_position
    """)
    tables = {}
    for r in rows:
        tn = r['table_name']
        if tn not in tables:
            tables[tn] = {}
        col = {
            'data_type': r['udt_name'] if r['data_type'] == 'USER-DEFINED' else r['data_type'],
            'max_length': r['character_maximum_length'],
            'numeric_precision': r['numeric_precision'],
            'numeric_scale': r['numeric_scale'],
            'is_nullable': r['is_nullable'],
            'column_default': r['column_default'],
            'ordinal_position': r['ordinal_position'],
        }
        tables[tn][r['column_name']] = col
    return tables

def get_views(conn):
    rows = fetch_all(conn, """
        SELECT table_name, view_definition
        FROM information_schema.views
        WHERE table_schema = 'public'
        ORDER BY table_name
    """)
    return {r['table_name']: r['view_definition'] for r in rows}

def get_functions(conn):
    rows = fetch_all(conn, """
        SELECT p.proname                                  AS func_name,
               pg_get_function_identity_arguments(p.oid) AS arguments,
               pg_get_functiondef(p.oid)                 AS definition,
               l.lanname                                  AS language,
               t.typname                                  AS return_type
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language  l ON l.oid = p.prolang
        JOIN pg_type      t ON t.oid = p.prorettype
        WHERE n.nspname = 'public'
          AND p.prokind IN ('f','p')
        ORDER BY p.proname
    """)
    funcs = {}
    for r in rows:
        key = f"{r['func_name']}({r['arguments']})"
        funcs[key] = {
            'language': r['language'],
            'return_type': r['return_type'],
            'definition': r['definition'],
        }
    return funcs

def get_indexes(conn):
    rows = fetch_all(conn, """
        SELECT schemaname, tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY tablename, indexname
    """)
    return {r['indexname']: {'table': r['tablename'], 'definition': r['indexdef']} for r in rows}

# ─────────────────────────────────────────────
# Diff helpers
# ─────────────────────────────────────────────

def diff_dicts(src, tgt):
    src_keys = set(src.keys())
    tgt_keys = set(tgt.keys())

    only_src = sorted(src_keys - tgt_keys)
    only_tgt = sorted(tgt_keys - src_keys)
    modified = []

    for k in sorted(src_keys & tgt_keys):
        if src[k] != tgt[k]:
            modified.append({'name': k, 'src': src[k], 'tgt': tgt[k]})

    return only_src, only_tgt, modified

def diff_tables(src_tables, tgt_tables):
    only_src_tables = sorted(set(src_tables) - set(tgt_tables))
    only_tgt_tables = sorted(set(tgt_tables) - set(src_tables))
    changed_tables = []

    for tname in sorted(set(src_tables) & set(tgt_tables)):
        src_cols = src_tables[tname]
        tgt_cols = tgt_tables[tname]
        only_src_cols, only_tgt_cols, modified_cols = diff_dicts(src_cols, tgt_cols)
        if only_src_cols or only_tgt_cols or modified_cols:
            changed_tables.append({
                'table': tname,
                'only_src': only_src_cols,
                'only_tgt': only_tgt_cols,
                'modified': modified_cols,
            })

    return {
        'only_src_tables': only_src_tables,
        'only_tgt_tables': only_tgt_tables,
        'changed_tables': changed_tables,
    }

# ─────────────────────────────────────────────
# DDL generators
# ─────────────────────────────────────────────

def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'

def col_type_str(col):
    t = col['data_type']
    if col['max_length']:
        return f"{t}({col['max_length']})"
    if col['numeric_precision'] is not None and t in ('numeric', 'decimal'):
        if col['numeric_scale'] is not None:
            return f"{t}({col['numeric_precision']},{col['numeric_scale']})"
        return f"{t}({col['numeric_precision']})"
    return t

def generate_create_table_sql(table_name, columns):
    cols_sorted = sorted(columns.items(), key=lambda x: x[1]['ordinal_position'])
    col_defs = []
    for col_name, col in cols_sorted:
        defn = f"  {quote_ident(col_name)} {col_type_str(col)}"
        if col['column_default'] is not None:
            defn += f" DEFAULT {col['column_default']}"
        if col['is_nullable'] == 'NO':
            defn += " NOT NULL"
        col_defs.append(defn)
    return f"CREATE TABLE {quote_ident(table_name)} (\n" + ",\n".join(col_defs) + "\n);"

def generate_add_column_sql(table_name, col_name, col):
    sql = f"ALTER TABLE {quote_ident(table_name)} ADD COLUMN {quote_ident(col_name)} {col_type_str(col)}"
    if col['column_default'] is not None:
        sql += f" DEFAULT {col['column_default']}"
    if col['is_nullable'] == 'NO':
        sql += " NOT NULL"
    return sql + ";"

def generate_alter_column_sqls(table_name, col_name, col):
    tq = quote_ident(table_name)
    cq = quote_ident(col_name)
    ts = col_type_str(col)
    sqls = [f"ALTER TABLE {tq} ALTER COLUMN {cq} TYPE {ts} USING {cq}::{ts};"]
    if col['column_default'] is not None:
        sqls.append(f"ALTER TABLE {tq} ALTER COLUMN {cq} SET DEFAULT {col['column_default']};")
    else:
        sqls.append(f"ALTER TABLE {tq} ALTER COLUMN {cq} DROP DEFAULT;")
    if col['is_nullable'] == 'NO':
        sqls.append(f"ALTER TABLE {tq} ALTER COLUMN {cq} SET NOT NULL;")
    else:
        sqls.append(f"ALTER TABLE {tq} ALTER COLUMN {cq} DROP NOT NULL;")
    return sqls

def generate_ddl(item, src_tables, src_views, src_functions, src_indexes):
    itype  = item['type']
    action = item['action']

    if itype == 'table' and action == 'create':
        tname = item['name']
        return [generate_create_table_sql(tname, src_tables[tname])]

    if itype == 'column' and action == 'add':
        tname, cname = item['table'], item['column']
        return [generate_add_column_sql(tname, cname, src_tables[tname][cname])]

    if itype == 'column' and action == 'modify':
        tname, cname = item['table'], item['column']
        return generate_alter_column_sqls(tname, cname, src_tables[tname][cname])

    if itype == 'view':
        vname = item['name']
        return [f"CREATE OR REPLACE VIEW {quote_ident(vname)} AS {src_views[vname]};"]

    if itype == 'function':
        fname = item['name']
        return [src_functions[fname]['definition'] + ";"]

    if itype == 'index' and action == 'create':
        iname = item['name']
        return [src_indexes[iname]['definition'] + ";"]

    if itype == 'index' and action == 'replace':
        iname = item['name']
        return [
            f"DROP INDEX IF EXISTS {quote_ident(iname)};",
            src_indexes[iname]['definition'] + ";"
        ]

    raise ValueError(f"Unknown item type/action: {itype}/{action}")

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/compare', methods=['POST'])
def compare():
    data = request.json
    src_dsn = data.get('src_dsn', '').strip()
    tgt_dsn = data.get('tgt_dsn', '').strip()

    if not src_dsn or not tgt_dsn:
        return jsonify({'error': 'Both connection strings are required.'}), 400

    try:
        src_conn = get_conn(src_dsn)
    except Exception as e:
        return jsonify({'error': f'Source connection failed: {e}'}), 400

    try:
        tgt_conn = get_conn(tgt_dsn)
    except Exception as e:
        src_conn.close()
        return jsonify({'error': f'Target connection failed: {e}'}), 400

    try:
        result = {}

        src_tables = get_tables(src_conn)
        tgt_tables = get_tables(tgt_conn)
        result['tables'] = diff_tables(src_tables, tgt_tables)

        src_views = get_views(src_conn)
        tgt_views = get_views(tgt_conn)
        os_, ot, mod = diff_dicts(src_views, tgt_views)
        result['views'] = {'only_src': os_, 'only_tgt': ot, 'modified': mod}

        src_fns = get_functions(src_conn)
        tgt_fns = get_functions(tgt_conn)
        os_, ot, mod = diff_dicts(src_fns, tgt_fns)
        result['functions'] = {'only_src': os_, 'only_tgt': ot, 'modified': mod}

        src_idx = get_indexes(src_conn)
        tgt_idx = get_indexes(tgt_conn)
        os_, ot, mod = diff_dicts(src_idx, tgt_idx)
        result['indexes'] = {'only_src': os_, 'only_tgt': ot, 'modified': mod}

        result['summary'] = {
            'tables':    len(result['tables']['only_src_tables']) + len(result['tables']['only_tgt_tables']) + len(result['tables']['changed_tables']),
            'views':     len(result['views']['only_src']) + len(result['views']['only_tgt']) + len(result['views']['modified']),
            'functions': len(result['functions']['only_src']) + len(result['functions']['only_tgt']) + len(result['functions']['modified']),
            'indexes':   len(result['indexes']['only_src']) + len(result['indexes']['only_tgt']) + len(result['indexes']['modified']),
        }

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'Comparison failed: {e}'}), 500
    finally:
        src_conn.close()
        tgt_conn.close()


@app.route('/api/apply', methods=['POST'])
def apply_changes():
    data     = request.json
    src_dsn  = data.get('src_dsn', '').strip()
    tgt_dsn  = data.get('tgt_dsn', '').strip()
    items    = data.get('items', [])
    dry_run  = data.get('dry_run', False)

    if not src_dsn or not tgt_dsn:
        return jsonify({'error': 'Both connection strings are required.'}), 400
    if not items:
        return jsonify({'error': 'No items selected.'}), 400

    try:
        src_conn = get_conn(src_dsn)
    except Exception as e:
        return jsonify({'error': f'Source connection failed: {e}'}), 400

    try:
        src_tables = src_views = src_functions = src_indexes = None
        for item in items:
            t = item['type']
            if t in ('table', 'column') and src_tables    is None: src_tables    = get_tables(src_conn)
            if t == 'view'              and src_views     is None: src_views     = get_views(src_conn)
            if t == 'function'          and src_functions is None: src_functions = get_functions(src_conn)
            if t == 'index'             and src_indexes   is None: src_indexes   = get_indexes(src_conn)

        plan = []
        for item in items:
            try:
                sqls = generate_ddl(item, src_tables, src_views, src_functions, src_indexes)
                plan.append({'item': item, 'sqls': sqls, 'error': None})
            except Exception as e:
                plan.append({'item': item, 'sqls': [], 'error': str(e)})

        if dry_run:
            return jsonify({'plan': plan})

        try:
            tgt_conn = get_conn(tgt_dsn)
        except Exception as e:
            return jsonify({'error': f'Target connection failed: {e}'}), 400

        results = []
        try:
            for entry in plan:
                if entry['error']:
                    results.append({'item': entry['item'], 'status': 'error', 'sqls': [], 'error': entry['error']})
                    continue
                try:
                    with tgt_conn.cursor() as cur:
                        for sql in entry['sqls']:
                            cur.execute(sql)
                    tgt_conn.commit()
                    results.append({'item': entry['item'], 'status': 'ok', 'sqls': entry['sqls'], 'error': None})
                except Exception as e:
                    tgt_conn.rollback()
                    results.append({'item': entry['item'], 'status': 'error', 'sqls': entry['sqls'], 'error': str(e)})
        finally:
            tgt_conn.close()

        return jsonify({'results': results})

    finally:
        src_conn.close()

# ─────────────────────────────────────────────
# Embedded HTML UI
# ─────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PG Schema Diff</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
<style>
  :root {
    --bg:       #0d0f14;
    --surface:  #13161e;
    --border:   #1e2330;
    --accent:   #00e5a0;
    --dev:      #f0c060;
    --uat:      #60a8f0;
    --mod:      #f06090;
    --text:     #c8cdd8;
    --muted:    #5a6070;
    --radius:   6px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Syne', sans-serif; min-height: 100vh; }

  /* ── Header ── */
  header {
    border-bottom: 1px solid var(--border);
    padding: 22px 40px;
    display: flex; align-items: center; gap: 16px;
  }
  .logo { font-size: 22px; font-weight: 800; letter-spacing: -0.5px; color: #fff; }
  .logo span { color: var(--accent); }
  .badge { font-family: 'JetBrains Mono', monospace; font-size: 11px; background: var(--border); color: var(--muted); padding: 3px 8px; border-radius: 20px; }

  /* ── Layout ── */
  main { max-width: 1100px; margin: 0 auto; padding: 36px 40px; }

  /* ── Connection Card ── */
  .conn-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 28px 32px; margin-bottom: 36px;
  }
  .conn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .field-wrap label { display: block; font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
  .field-wrap label .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dev-dot { background: var(--dev); }
  .uat-dot { background: var(--uat); }
  input[type=text], input[type=password] {
    width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    color: var(--text); font-family: 'JetBrains Mono', monospace; font-size: 13px;
    padding: 11px 14px; outline: none; transition: border-color .2s;
  }
  input:focus { border-color: var(--accent); }
  .btn-run {
    background: var(--accent); color: #0a0c10; font-family: 'Syne', sans-serif;
    font-weight: 700; font-size: 14px; letter-spacing: .3px;
    border: none; border-radius: var(--radius); padding: 12px 32px; cursor: pointer;
    transition: opacity .15s, transform .1s;
  }
  .btn-run:hover { opacity: .88; }
  .btn-run:active { transform: scale(.98); }
  .btn-run:disabled { opacity: .4; cursor: not-allowed; }

  /* ── Error banner ── */
  .error-box { background: #2a1018; border: 1px solid #6a2030; color: #f07090; border-radius: var(--radius); padding: 12px 16px; margin-top: 16px; font-family: 'JetBrains Mono', monospace; font-size: 13px; }

  /* ── Summary badges ── */
  .summary-row { display: flex; gap: 12px; margin-bottom: 28px; flex-wrap: wrap; }
  .sum-badge {
    display: flex; align-items: center; gap: 10px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 18px; cursor: pointer; transition: border-color .15s;
  }
  .sum-badge:hover, .sum-badge.active { border-color: var(--accent); }
  .sum-badge .count { font-family: 'JetBrains Mono', monospace; font-size: 22px; font-weight: 700; color: #fff; }
  .sum-badge .lbl { font-size: 12px; color: var(--muted); }
  .sum-badge .dot-sm { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }

  /* ── Tabs ── */
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
  .tab {
    font-size: 13px; font-weight: 600; padding: 10px 20px; cursor: pointer;
    border-bottom: 2px solid transparent; color: var(--muted); transition: color .15s, border-color .15s;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: #fff; border-bottom-color: var(--accent); }

  /* ── Section labels ── */
  .section { margin-bottom: 24px; }
  .section-title {
    font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 1px; padding: 6px 12px;
    border-radius: 4px; display: inline-flex; align-items: center; gap: 8px;
    margin-bottom: 12px;
  }
  .only-dev  { background: rgba(240,192,96,.1);  color: var(--dev); }
  .only-uat  { background: rgba(96,168,240,.1);  color: var(--uat); }
  .modified  { background: rgba(240,96,144,.1);  color: var(--mod); }
  .unchanged { background: rgba(0,229,160,.08);  color: var(--accent); }

  /* ── Item cards ── */
  .item-list { display: flex; flex-direction: column; gap: 8px; }
  .item {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 12px 16px; font-family: 'JetBrains Mono', monospace; font-size: 13px;
  }
  .item.dev-item  { border-left: 3px solid var(--dev); }
  .item.uat-item  { border-left: 3px solid var(--uat); }
  .item.mod-item  { border-left: 3px solid var(--mod); }
  .item-name { font-weight: 600; color: #fff; margin-bottom: 4px; }
  .item-meta { font-size: 11px; color: var(--muted); }

  /* ── Expandable diff ── */
  .diff-toggle { background: none; border: none; color: var(--accent); font-family: 'JetBrains Mono', monospace; font-size: 11px; cursor: pointer; margin-top: 6px; padding: 0; }
  .diff-content { display: none; margin-top: 10px; }
  .diff-content.open { display: block; }
  .diff-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .diff-table th { text-align: left; color: var(--muted); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--border); }
  .diff-table td { padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  .diff-table tr:last-child td { border-bottom: none; }
  .val-dev  { color: var(--dev); }
  .val-uat  { color: var(--uat); }

  /* ── Definition diff ── */
  .def-wrap { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px; }
  .def-box { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 10px 12px; }
  .def-box pre { font-size: 11px; white-space: pre-wrap; word-break: break-all; color: var(--text); line-height: 1.6; }
  .def-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; }

  /* ── Table diff ── */
  .table-block { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 12px; overflow: hidden; }
  .table-header { padding: 12px 16px; background: rgba(255,255,255,.02); display: flex; align-items: center; gap: 10px; cursor: pointer; }
  .table-header span { font-family: 'JetBrains Mono', monospace; font-weight: 600; color: #fff; font-size: 13px; }
  .table-body { padding: 0 16px 16px; }
  .col-row { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--border); font-family: 'JetBrains Mono', monospace; font-size: 12px; }
  .col-row:last-child { border-bottom: none; }
  .col-name { min-width: 180px; color: #fff; font-weight: 600; }
  .col-detail { color: var(--muted); font-size: 11px; }

  /* ── Empty state ── */
  .empty { text-align: center; padding: 60px 20px; color: var(--muted); font-size: 14px; }
  .empty .icon { font-size: 36px; margin-bottom: 12px; }

  /* ── Spinner ── */
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(0,229,160,.2); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── All clear ── */
  .all-clear { background: rgba(0,229,160,.07); border: 1px solid rgba(0,229,160,.2); border-radius: 8px; padding: 24px; text-align: center; color: var(--accent); font-size: 15px; font-weight: 600; }
  .all-clear .ic { font-size: 32px; margin-bottom: 8px; }

  /* ── PDF export button ── */
  .results-toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
  .btn-pdf {
    display: inline-flex; align-items: center; gap: 8px;
    background: transparent; border: 1px solid var(--border); color: var(--text);
    font-family: 'Syne', sans-serif; font-weight: 600; font-size: 13px;
    border-radius: var(--radius); padding: 9px 18px; cursor: pointer;
    transition: border-color .15s, color .15s;
  }
  .btn-pdf:hover { border-color: var(--accent); color: var(--accent); }
  .btn-pdf:disabled { opacity: .4; cursor: not-allowed; }
  .btn-pdf svg { width: 15px; height: 15px; }

  /* ── PDF hidden render area ── */
  #pdf-render { position: fixed; top: 0; left: 0; width: 900px; opacity: 0; pointer-events: none; background: #fff; font-family: 'JetBrains Mono', monospace; color: #1a1a2e; z-index: -1; }
  .pdf-header { padding: 32px 40px 20px; border-bottom: 3px solid #1a1a2e; display: flex; align-items: flex-start; justify-content: space-between; }
  .pdf-logo { font-family: 'Syne', sans-serif; font-size: 26px; font-weight: 800; color: #1a1a2e; }
  .pdf-logo span { color: #00b57a; }
  .pdf-meta { font-size: 11px; color: #666; text-align: right; line-height: 1.8; }
  .pdf-summary { display: flex; gap: 16px; padding: 20px 40px; background: #f7f8fa; border-bottom: 1px solid #e0e0e8; }
  .pdf-sum-item { flex: 1; text-align: center; }
  .pdf-sum-num { font-size: 26px; font-weight: 700; color: #1a1a2e; }
  .pdf-sum-lbl { font-size: 11px; color: #888; letter-spacing: .5px; text-transform: uppercase; margin-top: 2px; }
  .pdf-section { padding: 24px 40px; border-bottom: 1px solid #e8e8f0; }
  .pdf-section-title { font-family: 'Syne', sans-serif; font-size: 15px; font-weight: 800; color: #1a1a2e; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 2px solid #e0e0e8; display: flex; align-items: center; gap: 10px; }
  .pdf-sub-title { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; padding: 4px 10px; border-radius: 4px; margin-bottom: 8px; margin-top: 16px; display: inline-block; }
  .pdf-only-dev { background: #fff8e6; color: #b07800; border: 1px solid #f0c060; }
  .pdf-only-uat { background: #e8f2ff; color: #1a5fa8; border: 1px solid #60a8f0; }
  .pdf-modified { background: #fff0f4; color: #a02040; border: 1px solid #f06090; }
  .pdf-ok       { background: #e8fff6; color: #007a50; border: 1px solid #00c87a; }
  .pdf-item { font-size: 12px; padding: 7px 12px; margin-bottom: 5px; border-radius: 4px; border-left: 3px solid; }
  .pdf-item-dev { background: #fffbf0; border-left-color: #f0c060; }
  .pdf-item-uat { background: #f0f6ff; border-left-color: #60a8f0; }
  .pdf-item-mod { background: #fff4f7; border-left-color: #f06090; }
  .pdf-item-name { font-weight: 700; color: #1a1a2e; }
  .pdf-item-detail { font-size: 10px; color: #888; margin-top: 2px; }
  .pdf-diff-table { width: 100%; border-collapse: collapse; font-size: 11px; margin-top: 6px; }
  .pdf-diff-table th { background: #f0f0f8; padding: 4px 8px; text-align: left; font-weight: 600; color: #444; border: 1px solid #ddd; }
  .pdf-diff-table td { padding: 4px 8px; border: 1px solid #ddd; vertical-align: top; }
  .pdf-val-dev { color: #b07800; font-weight: 600; }
  .pdf-val-uat { color: #1a5fa8; font-weight: 600; }
  .pdf-def-wrap { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 8px; }
  .pdf-def-box { background: #f8f8fc; border: 1px solid #ddd; border-radius: 4px; padding: 8px; }
  .pdf-def-box pre { font-size: 9px; white-space: pre-wrap; word-break: break-all; color: #333; line-height: 1.5; }
  .pdf-def-label { font-size: 9px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 4px; }
  .pdf-all-clear { background: #e8fff6; border: 1px solid #00c87a; border-radius: 6px; padding: 14px 18px; color: #007a50; font-size: 12px; font-weight: 600; }
  .pdf-table-block { border: 1px solid #e0e0e8; border-radius: 4px; margin-bottom: 10px; overflow: hidden; }
  .pdf-table-header { background: #f0f0f8; padding: 8px 14px; font-weight: 700; font-size: 12px; color: #1a1a2e; border-bottom: 1px solid #e0e0e8; }
  .pdf-table-body { padding: 8px 14px; }
  .pdf-col-row { display: flex; gap: 12px; padding: 4px 0; border-bottom: 1px solid #f0f0f8; font-size: 11px; }
  .pdf-col-row:last-child { border-bottom: none; }
  .pdf-col-name { min-width: 180px; font-weight: 600; color: #1a1a2e; }
  .pdf-col-detail { color: #888; font-size: 10px; }
  .pdf-legend { display: flex; gap: 16px; padding: 12px 40px; background: #f7f8fa; border-bottom: 1px solid #e0e0e8; font-size: 11px; }
  .pdf-legend-item { display: flex; align-items: center; gap: 6px; color: #555; }
  .pdf-legend-dot { width: 10px; height: 10px; border-radius: 2px; }

  /* ── Checkboxes ── */
  .item-check {
    flex-shrink: 0; width: 16px; height: 16px; cursor: pointer;
    accent-color: var(--accent); margin-top: 1px;
  }
  .item-checkable { display: flex; align-items: flex-start; gap: 12px; }
  .col-row.checkable { align-items: center; }

  /* ── Apply bar ── */
  .apply-bar {
    position: fixed; bottom: 0; left: 0; right: 0;
    background: var(--surface); border-top: 2px solid var(--accent);
    padding: 14px 40px; display: flex; align-items: center;
    justify-content: space-between; z-index: 100;
    box-shadow: 0 -4px 20px rgba(0,0,0,.4);
  }
  .apply-bar-info { font-size: 14px; color: var(--accent); font-weight: 600; }
  .apply-bar-info span { color: var(--text); font-size: 12px; margin-left: 8px; }
  .btn-apply {
    background: var(--accent); color: #0a0c10;
    font-family: 'Syne', sans-serif; font-weight: 700; font-size: 14px;
    border: none; border-radius: var(--radius); padding: 10px 24px; cursor: pointer;
    transition: opacity .15s;
  }
  .btn-apply:hover { opacity: .85; }

  /* ── Modal ── */
  .modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
  }
  .modal-box {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 28px 32px; max-width: 720px; width: 92%;
    display: flex; flex-direction: column; max-height: 85vh;
  }
  .modal-title {
    font-size: 17px; font-weight: 700; color: #fff; margin-bottom: 18px;
    border-bottom: 1px solid var(--border); padding-bottom: 14px;
  }
  .modal-body { overflow-y: auto; flex: 1; }
  .modal-footer {
    display: flex; justify-content: flex-end; gap: 12px;
    margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border);
  }
  .btn-cancel {
    background: transparent; border: 1px solid var(--border); color: var(--text);
    font-family: 'Syne', sans-serif; font-weight: 600; font-size: 13px;
    border-radius: var(--radius); padding: 9px 20px; cursor: pointer;
    transition: border-color .15s;
  }
  .btn-cancel:hover { border-color: var(--text); }
  .btn-confirm {
    background: var(--accent); color: #0a0c10;
    font-family: 'Syne', sans-serif; font-weight: 700; font-size: 13px;
    border: none; border-radius: var(--radius); padding: 9px 20px; cursor: pointer;
    transition: opacity .15s;
  }
  .btn-confirm:hover { opacity: .85; }
  .btn-confirm:disabled { opacity: .4; cursor: not-allowed; }

  /* ── Modal content ── */
  .warning-box {
    background: rgba(240,192,96,.1); border: 1px solid rgba(240,192,96,.3);
    color: var(--dev); border-radius: var(--radius); padding: 10px 14px;
    font-size: 12px; margin-bottom: 14px; line-height: 1.5;
  }
  .plan-item {
    border: 1px solid var(--border); border-radius: var(--radius);
    padding: 10px 14px; margin-bottom: 8px;
  }
  .plan-item.plan-error { border-color: rgba(240,96,144,.4); }
  .plan-label { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #fff; font-weight: 600; margin-bottom: 6px; }
  .plan-sql {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 8px 12px; font-size: 11px; white-space: pre-wrap; word-break: break-all;
    color: var(--accent); line-height: 1.5; margin: 0;
  }
  .plan-err { font-size: 11px; color: var(--mod); margin-top: 4px; }
  .result-item {
    display: flex; align-items: flex-start; gap: 10px;
    font-family: 'JetBrains Mono', monospace; font-size: 12px;
    padding: 8px 0; border-bottom: 1px solid var(--border);
  }
  .result-item:last-child { border-bottom: none; }
  .result-ok  { color: var(--accent); }
  .result-err { color: var(--mod); }
  .result-icon { font-size: 14px; flex-shrink: 0; }
  .result-err-msg { font-size: 11px; color: var(--muted); margin-top: 3px; }
  .results-summary { font-size: 13px; color: var(--text); margin-bottom: 12px; font-weight: 600; }
  .rerun-hint { font-size: 12px; color: var(--muted); margin-top: 12px; text-align: center; }
  .modal-loading { text-align: center; padding: 30px; color: var(--muted); font-size: 14px; }
</style>
</head>
<body>

<header>
  <div class="logo">PG<span>Diff</span></div>
  <div class="badge">PostgreSQL Schema Comparator</div>
</header>

<main>

  <!-- Connection form -->
  <div class="conn-card">
    <div class="conn-grid">
      <div class="field-wrap">
        <label><span class="dot dev-dot"></span>Source Database</label>
        <input type="text" id="srcDsn" placeholder="postgresql://user:pass@host:5432/source_db"/>
      </div>
      <div class="field-wrap">
        <label><span class="dot uat-dot"></span>Target Database</label>
        <input type="text" id="tgtDsn" placeholder="postgresql://user:pass@host:5432/target_db"/>
      </div>
    </div>
    <button class="btn-run" id="runBtn" onclick="runCompare()">Compare Schemas</button>
    <div id="errorBox" class="error-box" style="display:none"></div>
  </div>

  <!-- Results -->
  <div id="results" style="display:none">

    <!-- Toolbar -->
    <div class="results-toolbar">
      <div class="summary-row" id="summaryRow" style="margin-bottom:0"></div>
      <button class="btn-pdf" id="pdfBtn" onclick="exportPDF()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
          <line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 18 15 15"/>
        </svg>
        Export PDF
      </button>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      <div class="tab active" onclick="showTab('tables')"   id="tab-tables">   Tables &amp; Columns</div>
      <div class="tab"        onclick="showTab('views')"    id="tab-views">    Views</div>
      <div class="tab"        onclick="showTab('functions')" id="tab-functions">Functions</div>
      <div class="tab"        onclick="showTab('indexes')"  id="tab-indexes">  Indexes</div>
    </div>

    <div id="pane-tables"    class="pane"></div>
    <div id="pane-views"     class="pane" style="display:none"></div>
    <div id="pane-functions" class="pane" style="display:none"></div>
    <div id="pane-indexes"   class="pane" style="display:none"></div>
  </div>

</main>

<!-- Apply bar -->
<div id="apply-bar" class="apply-bar" style="display:none">
  <div class="apply-bar-info">
    <span id="apply-count">0</span> item(s) selected
    <span>Changes will be applied to Target database</span>
  </div>
  <button class="btn-apply" onclick="showApplyModal()">Apply to Target →</button>
</div>

<!-- Modal -->
<div id="apply-modal" class="modal-overlay" style="display:none">
  <div class="modal-box">
    <div class="modal-title" id="modal-title">Apply Changes to Target</div>
    <div class="modal-body" id="modal-body"></div>
    <div class="modal-footer" id="modal-footer"></div>
  </div>
</div>

<!-- Hidden PDF render area -->
<div id="pdf-render"></div>

<script>
let DATA = null;
let SRC_LABEL = 'Source';
let TGT_LABEL = 'Target';
let selectedItems = new Map();

function showTab(name) {
  ['tables','views','functions','indexes'].forEach(t => {
    document.getElementById('pane-'+t).style.display = t===name ? '' : 'none';
    document.getElementById('tab-'+t).className = 'tab' + (t===name ? ' active' : '');
  });
}

async function runCompare() {
  const src = document.getElementById('srcDsn').value.trim();
  const tgt = document.getElementById('tgtDsn').value.trim();
  const btn = document.getElementById('runBtn');
  const errBox = document.getElementById('errorBox');

  errBox.style.display = 'none';
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Comparing…';
  selectedItems.clear();
  updateApplyBar();

  try {
    const res = await fetch('/api/compare', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({src_dsn: src, tgt_dsn: tgt})
    });
    const data = await res.json();
    if (!res.ok || data.error) { showError(data.error || 'Unknown error'); return; }
    DATA = data;
    SRC_LABEL = src.split('/').pop() || 'Source';
    TGT_LABEL = tgt.split('/').pop() || 'Target';
    renderAll(data);
    document.getElementById('results').style.display = '';
  } catch(e) {
    showError('Request failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Compare Schemas';
  }
}

function showError(msg) {
  const b = document.getElementById('errorBox');
  b.textContent = '⚠ ' + msg;
  b.style.display = '';
}

// ─── Render summary ───
function renderAll(data) {
  renderSummary(data.summary);
  renderTablesPane(data.tables);
  renderSimplePane('pane-views',     data.views,     'View',     'view');
  renderSimplePane('pane-functions', data.functions, 'Function', 'function');
  renderSimplePane('pane-indexes',   data.indexes,   'Index',    'index');
}

function renderSummary(s) {
  const row = document.getElementById('summaryRow');
  const tabs = [
    {key:'tables',    label:'Tables'},
    {key:'views',     label:'Views'},
    {key:'functions', label:'Functions'},
    {key:'indexes',   label:'Indexes'},
  ];
  row.innerHTML = tabs.map(t => `
    <div class="sum-badge" onclick="showTab('${t.key}')">
      <div class="dot-sm"></div>
      <div>
        <div class="count">${s[t.key]}</div>
        <div class="lbl">${t.label} diff${s[t.key]!==1?'s':''}</div>
      </div>
    </div>`).join('');
}

// ─── Checkbox helpers ───
function checkbox(item) {
  const j = JSON.stringify(item).replace(/'/g, '&#39;');
  return `<input type="checkbox" class="item-check" data-item='${j}' onchange="toggleItem(this)">`;
}

function toggleItem(el) {
  const item = JSON.parse(el.dataset.item);
  const key  = itemKey(item);
  if (el.checked) selectedItems.set(key, item);
  else selectedItems.delete(key);
  updateApplyBar();
}

function itemKey(item) {
  if (item.type === 'table')    return `table:${item.name}`;
  if (item.type === 'column')   return `column:${item.table}:${item.column}`;
  if (item.type === 'view')     return `view:${item.name}`;
  if (item.type === 'function') return `function:${item.name}`;
  if (item.type === 'index')    return `index:${item.name}`;
  return JSON.stringify(item);
}

function updateApplyBar() {
  const bar   = document.getElementById('apply-bar');
  const count = selectedItems.size;
  document.getElementById('apply-count').textContent = count;
  bar.style.display = count > 0 ? 'flex' : 'none';
}

// ─── Tables pane ───
function renderTablesPane(tables) {
  const el = document.getElementById('pane-tables');
  let html = '';
  const total = tables.only_src_tables.length + tables.only_tgt_tables.length + tables.changed_tables.length;
  if (total === 0) { el.innerHTML = allClear('Tables &amp; Columns'); return; }

  if (tables.only_src_tables.length) {
    html += section('only-dev', `▲ Only in Source — needs to be added to Target`, tables.only_src_tables.map(t => {
      const item = {type:'table', action:'create', name:t};
      return `<div class="item dev-item item-checkable">
        ${checkbox(item)}
        <div><div class="item-name">${esc(t)}</div><div class="item-meta">Table missing from Target</div></div>
      </div>`;
    }).join(''));
  }
  if (tables.only_tgt_tables.length) {
    html += section('only-uat', '▼ Only in Target — not present in Source', tables.only_tgt_tables.map(t =>
      `<div class="item uat-item"><div class="item-name">${esc(t)}</div><div class="item-meta">Table missing from Source</div></div>`
    ).join(''));
  }
  if (tables.changed_tables.length) {
    html += `<div class="section"><div class="section-title modified">≠ Column differences (${tables.changed_tables.length} table${tables.changed_tables.length!==1?'s':''})</div>`;
    tables.changed_tables.forEach((td, i) => {
      const id = 'tb'+i;
      let inner = '';
      if (td.only_src.length) inner += colGroupSrc(td.table, td.only_src);
      if (td.only_tgt.length) inner += colGroupTgt(td.only_tgt);
      if (td.modified.length) inner += modifiedCols(td.table, td.modified);
      html += `<div class="table-block">
        <div class="table-header" onclick="toggle('${id}')">
          <span>${esc(td.table)}</span>
          <span style="color:var(--muted);font-size:11px;font-family:'JetBrains Mono',monospace">
            ${[td.only_src.length&&td.only_src.length+' col(s) only in Source',
               td.only_tgt.length&&td.only_tgt.length+' col(s) only in Target',
               td.modified.length&&td.modified.length+' col(s) modified'].filter(Boolean).join(' · ')}
          </span>
        </div>
        <div class="table-body" id="${id}">${inner}</div>
      </div>`;
    });
    html += '</div>';
  }
  el.innerHTML = html;
}

function colGroupSrc(tableName, cols) {
  return cols.map(c => {
    const item = {type:'column', action:'add', table:tableName, column:c};
    return `<div class="col-row dev-item checkable">
      ${checkbox(item)}
      <div class="col-name">${esc(c)}</div>
      <div class="col-detail">▲ Source only</div>
    </div>`;
  }).join('');
}

function colGroupTgt(cols) {
  return cols.map(c =>
    `<div class="col-row uat-item"><div class="col-name">${esc(c)}</div><div class="col-detail">▼ Target only</div></div>`
  ).join('');
}

function modifiedCols(tableName, mods) {
  return mods.map(m => {
    const item = {type:'column', action:'modify', table:tableName, column:m.name};
    const rows = Object.keys(m.src).map(k => {
      if (m.src[k] === m.tgt[k]) return '';
      return `<tr><td>${k}</td><td class="val-dev">${fmt(m.src[k])}</td><td class="val-uat">${fmt(m.tgt[k])}</td></tr>`;
    }).filter(Boolean).join('');
    return `<div class="col-row mod-item checkable">
      ${checkbox(item)}
      <div style="width:100%">
        <div class="col-name">${esc(m.name)}</div>
        <table class="diff-table" style="margin-top:6px">
          <tr><th>Property</th><th style="color:var(--dev)">Source</th><th style="color:var(--uat)">Target</th></tr>
          ${rows}
        </table>
      </div>
    </div>`;
  }).join('');
}

// ─── Generic pane (views/functions/indexes) ───
function renderSimplePane(paneId, data, label, itemType) {
  const el = document.getElementById(paneId);
  const total = data.only_src.length + data.only_tgt.length + data.modified.length;
  if (total === 0) { el.innerHTML = allClear(label + 's'); return; }

  let html = '';
  if (data.only_src.length) {
    html += section('only-dev', `▲ Only in Source (${data.only_src.length})`,
      data.only_src.map(n => {
        const item = {type:itemType, action:'create', name:n};
        return `<div class="item dev-item item-checkable">
          ${checkbox(item)}
          <div><div class="item-name">${esc(n)}</div><div class="item-meta">Missing from Target</div></div>
        </div>`;
      }).join(''));
  }
  if (data.only_tgt.length) {
    html += section('only-uat', `▼ Only in Target (${data.only_tgt.length})`,
      data.only_tgt.map(n => `<div class="item uat-item"><div class="item-name">${esc(n)}</div><div class="item-meta">Missing from Source</div></div>`).join(''));
  }
  if (data.modified.length) {
    html += `<div class="section"><div class="section-title modified">≠ Modified (${data.modified.length})</div><div class="item-list">`;
    data.modified.forEach((m, i) => {
      const id   = paneId+'mod'+i;
      const item = {type:itemType, action:'replace', name:m.name};
      const isStr = typeof m.src === 'string';
      let diffHtml = '';
      if (isStr) {
        diffHtml = `<div class="def-wrap">
          <div class="def-box"><div class="def-label" style="color:var(--dev)">Source</div><pre>${esc(m.src||'')}</pre></div>
          <div class="def-box"><div class="def-label" style="color:var(--uat)">Target</div><pre>${esc(m.tgt||'')}</pre></div>
        </div>`;
      } else {
        const rows = Object.keys(m.src||{}).map(k => {
          const sv = JSON.stringify(m.src[k]); const tv = JSON.stringify(m.tgt[k]);
          if (sv===tv) return '';
          if (k==='definition') return '';
          return `<tr><td>${k}</td><td class="val-dev">${fmt(m.src[k])}</td><td class="val-uat">${fmt(m.tgt[k])}</td></tr>`;
        }).filter(Boolean).join('');
        const hasDef = m.src && m.src.definition && m.src.definition !== (m.tgt && m.tgt.definition);
        diffHtml = `
          ${rows ? `<table class="diff-table" style="margin-top:8px"><tr><th>Property</th><th style="color:var(--dev)">Source</th><th style="color:var(--uat)">Target</th></tr>${rows}</table>` : ''}
          ${hasDef ? `<div class="def-wrap" style="margin-top:10px">
            <div class="def-box"><div class="def-label" style="color:var(--dev)">Source Definition</div><pre>${esc(m.src.definition||'')}</pre></div>
            <div class="def-box"><div class="def-label" style="color:var(--uat)">Target Definition</div><pre>${esc(m.tgt.definition||'')}</pre></div>
          </div>` : ''}`;
      }
      html += `<div class="item mod-item item-checkable">
        ${checkbox(item)}
        <div style="width:100%">
          <div class="item-name">${esc(m.name)}</div>
          <button class="diff-toggle" onclick="toggle('${id}')">▸ show diff</button>
          <div class="diff-content" id="${id}">${diffHtml}</div>
        </div>
      </div>`;
    });
    html += '</div></div>';
  }
  el.innerHTML = html;
}

// ─── Utilities ───
function section(cls, title, content) {
  return `<div class="section"><div class="section-title ${cls}">${title}</div><div class="item-list">${content}</div></div>`;
}
function allClear(label) {
  return `<div class="all-clear"><div class="ic">✓</div>${label} are in sync between Source and Target</div>`;
}

// ─── Apply modal ───
function itemLabel(item) {
  if (item.type === 'table')    return `CREATE TABLE ${item.name}`;
  if (item.type === 'column' && item.action === 'add')    return `ADD COLUMN "${item.column}" to ${item.table}`;
  if (item.type === 'column' && item.action === 'modify') return `MODIFY COLUMN "${item.column}" in ${item.table}`;
  if (item.type === 'view')     return `${item.action === 'replace' ? 'REPLACE' : 'CREATE'} VIEW ${item.name}`;
  if (item.type === 'function') return `${item.action === 'replace' ? 'REPLACE' : 'CREATE'} FUNCTION ${item.name}`;
  if (item.type === 'index')    return `${item.action === 'replace' ? 'REPLACE' : 'CREATE'} INDEX ${item.name}`;
  return JSON.stringify(item);
}

async function showApplyModal() {
  if (!selectedItems.size) return;
  const modal = document.getElementById('apply-modal');
  modal.style.display = 'flex';
  setModalState('loading', 'Generating SQL preview…');

  try {
    const res = await fetch('/api/apply', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        src_dsn:  document.getElementById('srcDsn').value.trim(),
        tgt_dsn:  document.getElementById('tgtDsn').value.trim(),
        items:    Array.from(selectedItems.values()),
        dry_run:  true
      })
    });
    const data = await res.json();
    if (!res.ok || data.error) { setModalState('error', data.error || 'Preview failed'); return; }
    setModalState('preview', data.plan);
  } catch(e) {
    setModalState('error', 'Request failed: ' + e.message);
  }
}

async function confirmApply() {
  document.getElementById('modal-footer').querySelector('.btn-confirm').disabled = true;
  setModalState('loading', 'Applying changes to Target…');

  try {
    const res = await fetch('/api/apply', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        src_dsn:  document.getElementById('srcDsn').value.trim(),
        tgt_dsn:  document.getElementById('tgtDsn').value.trim(),
        items:    Array.from(selectedItems.values()),
        dry_run:  false
      })
    });
    const data = await res.json();
    if (!res.ok || data.error) { setModalState('error', data.error || 'Apply failed'); return; }
    setModalState('results', data.results);
  } catch(e) {
    setModalState('error', 'Request failed: ' + e.message);
  }
}

function setModalState(state, data) {
  const body   = document.getElementById('modal-body');
  const footer = document.getElementById('modal-footer');

  if (state === 'loading') {
    body.innerHTML   = `<div class="modal-loading"><span class="spinner"></span>${esc(data)}</div>`;
    footer.innerHTML = '';
    return;
  }
  if (state === 'error') {
    body.innerHTML   = `<div class="error-box">${esc(data)}</div>`;
    footer.innerHTML = `<button class="btn-cancel" onclick="closeModal()">Close</button>`;
    return;
  }
  if (state === 'preview') {
    const plan = data;
    const hasTableCreate = plan.some(e => e.item.type === 'table' && e.item.action === 'create');
    let html = '';
    if (hasTableCreate) {
      html += `<div class="warning-box">⚠ Tables are copied with column definitions only. Primary keys, foreign keys, constraints, and sequences are NOT included and must be applied separately.</div>`;
    }
    html += plan.map(entry => {
      if (entry.error) {
        return `<div class="plan-item plan-error">
          <div class="plan-label">${esc(itemLabel(entry.item))}</div>
          <div class="plan-err">${esc(entry.error)}</div>
        </div>`;
      }
      return `<div class="plan-item">
        <div class="plan-label">${esc(itemLabel(entry.item))}</div>
        <pre class="plan-sql">${entry.sqls.map(s => esc(s)).join('\n')}</pre>
      </div>`;
    }).join('');
    body.innerHTML   = html;
    footer.innerHTML = `
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-confirm" onclick="confirmApply()">Confirm &amp; Apply</button>`;
    return;
  }
  if (state === 'results') {
    const results = data;
    const ok  = results.filter(r => r.status === 'ok').length;
    const err = results.filter(r => r.status === 'error').length;
    let html = `<div class="results-summary">${ok} succeeded${err ? `, ${err} failed` : ''}</div>`;
    html += results.map(r => {
      if (r.status === 'ok') {
        return `<div class="result-item result-ok"><span class="result-icon">✓</span><div>${esc(itemLabel(r.item))}</div></div>`;
      }
      return `<div class="result-item result-err">
        <span class="result-icon">✗</span>
        <div>${esc(itemLabel(r.item))}<div class="result-err-msg">${esc(r.error)}</div></div>
      </div>`;
    }).join('');
    if (ok > 0) html += `<div class="rerun-hint">Run comparison again to verify the applied changes.</div>`;
    body.innerHTML   = html;
    footer.innerHTML = `<button class="btn-confirm" onclick="closeModal()">Done</button>`;
    if (ok > 0) { selectedItems.clear(); updateApplyBar(); }
    return;
  }
}

function closeModal() {
  document.getElementById('apply-modal').style.display = 'none';
}
function toggle(id) {
  const el = document.getElementById(id);
  el.classList.toggle('open');
  if (el.previousElementSibling && el.previousElementSibling.classList.contains('diff-toggle')) {
    el.previousElementSibling.textContent = el.classList.contains('open') ? '▾ hide diff' : '▸ show diff';
  }
}
function fmt(v) {
  if (v === null || v === undefined) return '<span style="color:var(--muted)">null</span>';
  return esc(String(v));
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ─── PDF Export ───
function buildPdfHtml() {
  const now = new Date().toLocaleString();
  const s = DATA.summary;

  let html = `
    <div class="pdf-header">
      <div><div class="pdf-logo">PG<span>Diff</span></div><div style="font-size:12px;color:#555;margin-top:4px">PostgreSQL Schema Comparison Report</div></div>
      <div class="pdf-meta">
        <div><b>Source:</b> ${esc(SRC_LABEL)}</div>
        <div><b>Target:</b> ${esc(TGT_LABEL)}</div>
        <div><b>Generated:</b> ${now}</div>
      </div>
    </div>
    <div class="pdf-summary">
      ${[['Tables',s.tables],['Views',s.views],['Functions',s.functions],['Indexes',s.indexes]].map(([l,n])=>`
        <div class="pdf-sum-item"><div class="pdf-sum-num">${n}</div><div class="pdf-sum-lbl">${l} diff${n!==1?'s':''}</div></div>`).join('')}
    </div>
    <div class="pdf-legend">
      <div class="pdf-legend-item"><div class="pdf-legend-dot" style="background:#f0c060"></div>Only in Source (needs to be applied to Target)</div>
      <div class="pdf-legend-item"><div class="pdf-legend-dot" style="background:#60a8f0"></div>Only in Target</div>
      <div class="pdf-legend-item"><div class="pdf-legend-dot" style="background:#f06090"></div>Modified (differences found)</div>
      <div class="pdf-legend-item"><div class="pdf-legend-dot" style="background:#00c87a"></div>In sync</div>
    </div>`;

  html += pdfSection('Tables &amp; Columns',       buildPdfTables(DATA.tables));
  html += pdfSection('Views',                       buildPdfSimple(DATA.views,     'View'));
  html += pdfSection('Functions &amp; Procedures',  buildPdfSimple(DATA.functions, 'Function'));
  html += pdfSection('Indexes',                     buildPdfSimple(DATA.indexes,   'Index'));

  return html;
}

// Uses the browser's native print engine. html2pdf/html2canvas keep producing
// blank canvases in this setup — the print pipeline uses the exact renderer
// that draws the page, so it is guaranteed to capture whatever you see.
async function exportPDF() {
  if (!DATA) { alert('Run a comparison first.'); return; }

  const btn = document.getElementById('pdfBtn');
  const originalBtnHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Preparing print…';

  const printContainer = document.createElement('div');
  printContainer.id = 'pdf-print-area';
  printContainer.innerHTML = buildPdfHtml();

  const printStyle = document.createElement('style');
  printStyle.id = 'pdf-print-style';
  printStyle.textContent = `
    /* Screen: hide the report — only used for printing */
    #pdf-print-area { display: none; }

    @media print {
      @page { size: A4; margin: 10mm; }

      /* Hide everything but our report */
      html, body { background: #ffffff !important; }
      body > * { display: none !important; }
      body > #pdf-print-area { display: block !important; }

      #pdf-print-area {
        position: static !important;
        width: 100% !important;
        background: #ffffff !important;
        color: #1a1a2e !important;
        font-family: 'JetBrains Mono', monospace;
      }

      /* Drop the dark overlay-y backgrounds the screen UI uses */
      #pdf-print-area, #pdf-print-area * {
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
      }
    }
  `;

  document.head.appendChild(printStyle);
  document.body.appendChild(printContainer);

  const originalTitle = document.title;
  const cleanup = () => {
    if (printContainer.parentNode) printContainer.parentNode.removeChild(printContainer);
    if (printStyle.parentNode)     printStyle.parentNode.removeChild(printStyle);
    document.title = originalTitle;
    btn.disabled  = false;
    btn.innerHTML = originalBtnHTML;
    window.removeEventListener('afterprint', onAfterPrint);
  };
  const onAfterPrint = () => cleanup();
  window.addEventListener('afterprint', onAfterPrint);

  try {
    // Browsers use document.title as the suggested filename for "Save as PDF"
    document.title = `pgdiff_${SRC_LABEL}_vs_${TGT_LABEL}_${new Date().toISOString().slice(0,10)}`;

    if (document.fonts && document.fonts.ready) {
      try { await document.fonts.ready; } catch(_) {}
    }
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

    window.print();

    // Safety: some browsers don't fire afterprint reliably (e.g. if dialog is cancelled)
    setTimeout(cleanup, 2000);
  } catch (err) {
    console.error('Print export failed:', err);
    alert('Print export failed: ' + (err && err.message ? err.message : err));
    cleanup();
  }
}

function pdfSection(title, content) {
  return `<div class="pdf-section"><div class="pdf-section-title">${title}</div>${content}</div>`;
}

function buildPdfTables(tables) {
  const total = tables.only_src_tables.length + tables.only_tgt_tables.length + tables.changed_tables.length;
  if (total === 0) return `<div class="pdf-all-clear">✓ Tables &amp; Columns are in sync</div>`;
  let html = '';

  if (tables.only_src_tables.length) {
    html += `<div class="pdf-sub-title pdf-only-dev">▲ Only in Source — must be added to Target (${tables.only_src_tables.length})</div>`;
    html += tables.only_src_tables.map(t => `
      <div class="pdf-item pdf-item-dev">
        <div class="pdf-item-name">${esc(t)}</div>
        <div class="pdf-item-detail">Table missing from Target</div>
      </div>`).join('');
  }
  if (tables.only_tgt_tables.length) {
    html += `<div class="pdf-sub-title pdf-only-uat">▼ Only in Target — not in Source (${tables.only_tgt_tables.length})</div>`;
    html += tables.only_tgt_tables.map(t => `
      <div class="pdf-item pdf-item-uat">
        <div class="pdf-item-name">${esc(t)}</div>
        <div class="pdf-item-detail">Table missing from Source</div>
      </div>`).join('');
  }
  if (tables.changed_tables.length) {
    html += `<div class="pdf-sub-title pdf-modified">≠ Column differences (${tables.changed_tables.length} table${tables.changed_tables.length!==1?'s':''})</div>`;
    tables.changed_tables.forEach(td => {
      let inner = '';
      if (td.only_src.length) inner += td.only_src.map(c => `
        <div class="pdf-col-row">
          <div class="pdf-col-name">${esc(c)}</div>
          <div class="pdf-col-detail pdf-val-dev">▲ Source only — missing from Target</div>
        </div>`).join('');
      if (td.only_tgt.length) inner += td.only_tgt.map(c => `
        <div class="pdf-col-row">
          <div class="pdf-col-name">${esc(c)}</div>
          <div class="pdf-col-detail pdf-val-uat">▼ Target only — missing from Source</div>
        </div>`).join('');
      if (td.modified.length) inner += td.modified.map(m => {
        const rows = Object.keys(m.src||{}).map(k => {
          if (m.src[k] === m.tgt[k]) return '';
          return `<tr><td>${esc(k)}</td><td class="pdf-val-dev">${pdfFmt(m.src[k])}</td><td class="pdf-val-uat">${pdfFmt(m.tgt[k])}</td></tr>`;
        }).filter(Boolean).join('');
        return `<div class="pdf-col-row" style="flex-direction:column">
          <div class="pdf-col-name" style="margin-bottom:4px">${esc(m.name)} <span style="color:#f06090;font-size:10px">modified</span></div>
          ${rows ? `<table class="pdf-diff-table"><tr><th>Property</th><th>Source</th><th>Target</th></tr>${rows}</table>` : ''}
        </div>`;
      }).join('');
      html += `<div class="pdf-table-block">
        <div class="pdf-table-header">${esc(td.table)}</div>
        <div class="pdf-table-body">${inner}</div>
      </div>`;
    });
  }
  return html;
}

function buildPdfSimple(data, label) {
  const total = data.only_src.length + data.only_tgt.length + data.modified.length;
  if (total === 0) return `<div class="pdf-all-clear">✓ ${label}s are in sync</div>`;
  let html = '';

  if (data.only_src.length) {
    html += `<div class="pdf-sub-title pdf-only-dev">▲ Only in Source — must be applied to Target (${data.only_src.length})</div>`;
    html += data.only_src.map(n => `
      <div class="pdf-item pdf-item-dev">
        <div class="pdf-item-name">${esc(n)}</div>
        <div class="pdf-item-detail">${label} missing from Target</div>
      </div>`).join('');
  }
  if (data.only_tgt.length) {
    html += `<div class="pdf-sub-title pdf-only-uat">▼ Only in Target (${data.only_tgt.length})</div>`;
    html += data.only_tgt.map(n => `
      <div class="pdf-item pdf-item-uat">
        <div class="pdf-item-name">${esc(n)}</div>
        <div class="pdf-item-detail">${label} missing from Source</div>
      </div>`).join('');
  }
  if (data.modified.length) {
    html += `<div class="pdf-sub-title pdf-modified">≠ Modified (${data.modified.length})</div>`;
    data.modified.forEach(m => {
      const isStr = typeof m.src === 'string';
      let diffHtml = '';
      if (isStr) {
        diffHtml = `<div class="pdf-def-wrap">
          <div class="pdf-def-box"><div class="pdf-def-label pdf-val-dev">Source</div><pre>${esc(m.src||'')}</pre></div>
          <div class="pdf-def-box"><div class="pdf-def-label pdf-val-uat">Target</div><pre>${esc(m.tgt||'')}</pre></div>
        </div>`;
      } else {
        const rows = Object.keys(m.src||{}).map(k => {
          if (JSON.stringify(m.src[k]) === JSON.stringify(m.tgt[k])) return '';
          if (k === 'definition') return '';
          return `<tr><td>${esc(k)}</td><td class="pdf-val-dev">${pdfFmt(m.src[k])}</td><td class="pdf-val-uat">${pdfFmt(m.tgt[k])}</td></tr>`;
        }).filter(Boolean).join('');
        const hasDef = m.src && m.src.definition && m.src.definition !== (m.tgt && m.tgt.definition);
        diffHtml = `
          ${rows ? `<table class="pdf-diff-table"><tr><th>Property</th><th>Source</th><th>Target</th></tr>${rows}</table>` : ''}
          ${hasDef ? `<div class="pdf-def-wrap">
            <div class="pdf-def-box"><div class="pdf-def-label pdf-val-dev">Source Definition</div><pre>${esc(m.src.definition||'')}</pre></div>
            <div class="pdf-def-box"><div class="pdf-def-label pdf-val-uat">Target Definition</div><pre>${esc(m.tgt.definition||'')}</pre></div>
          </div>` : ''}`;
      }
      html += `<div class="pdf-item pdf-item-mod">
        <div class="pdf-item-name">${esc(m.name)}</div>
        ${diffHtml}
      </div>`;
    });
  }
  return html;
}

function pdfFmt(v) {
  if (v === null || v === undefined) return '<span style="color:#aaa">null</span>';
  return esc(String(v));
}

// Enter key support
document.addEventListener('keydown', e => {
  if (e.key==='Enter') runCompare();
});
</script>
</body>
</html>"""

if __name__ == '__main__':
    print("\n  PGDiff running at  http://localhost:5050\n")
    app.run(host='0.0.0.0', port=5050, debug=False)