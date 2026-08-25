# -*- coding: utf-8 -*-
"""
每月自動更新分隊服務人口。

流程：
  1. 讀 index.html 目前的資料月份
  2. 向內政部戶政司開放資料查「有沒有更新的月份」（沒有就什麼都不做）
  3. 抓村里人口 → 套 data/village_map.csv → 加總到分隊
  4. 套 data/staff.csv 的人員數 → 改寫 index.html

只用標準函式庫，不需要 pip install。
"""
import csv
import datetime
import io
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'index.html')
VILLAGE_MAP = os.path.join(ROOT, 'data', 'village_map.csv')
STAFF = os.path.join(ROOT, 'data', 'staff.csv')
# 選用：Google 試算表「發布到網路」的 CSV 網址。有填就以試算表為準，
# 檢查通過後回寫 staff.csv；沒填就直接用 staff.csv。
SHEET_URL_FILE = os.path.join(ROOT, 'data', 'staff_sheet_url.txt')
SUMMARY = os.path.join(ROOT, '_update_summary.md')

API = 'https://www.ris.gov.tw/rs-opendata/api/v1/datastore/ODRP013/{month}?page={page}'
CITY = '臺南市'
MAX_LOOKBACK = 24          # 最多往回找幾個月，避免無限迴圈

norm = lambda s: unicodedata.normalize('NFC', s.strip())


# ── 民國年月工具 ──────────────────────────────────────────────────────
def roc_now():
    t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return (t.year - 1911) * 100 + t.month


def roc_next(m):
    y, mo = divmod(m, 100)
    return (y + 1) * 100 + 1 if mo >= 12 else y * 100 + mo + 1


def roc_label(m):
    y, mo = divmod(m, 100)
    return '民國%d年%d月' % (y, mo)


# ── 抓資料 ────────────────────────────────────────────────────────────
def fetch_month(month):
    """回傳 {里代碼: 人口} ；查無資料回 None。"""
    out, page, pages = {}, 1, 1
    while page <= pages:
        url = API.format(month=month, page=page)
        req = urllib.request.Request(url, headers={'User-Agent': 'tainan-fire-map/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode('utf-8'))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print('  抓取失敗 %s p%d: %s' % (month, page, e))
            return None
        if d.get('responseCode') != 'OD-0101-S':
            return None
        pages = int(d.get('totalPage') or 1)
        for row in d.get('responseData', []):
            if row['site_id'].startswith(CITY):
                out[row['district_code']] = int(row['people_total'])
        page += 1
    return out or None


def latest_available(from_month):
    """從今天所在月往回找，回傳 >= from_month 且抓得到的最新月份。"""
    m = roc_now()
    for _ in range(MAX_LOOKBACK):
        if m < from_month:
            return None, None
        print('  試 %s ...' % m, end=' ')
        data = fetch_month(m)
        if data:
            print('有資料（%d 里）' % len(data))
            return m, data
        print('查無資料')
        y, mo = divmod(m, 100)
        m = (y - 1) * 100 + 12 if mo <= 1 else y * 100 + mo - 1
    return None, None


# ── 讀設定檔 ──────────────────────────────────────────────────────────
def read_csv(path):
    with io.open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def read_sheet_url():
    """讀 data/staff_sheet_url.txt；沒有檔案、空的或整行是註解就回 None。"""
    if not os.path.exists(SHEET_URL_FILE):
        return None
    for line in io.open(SHEET_URL_FILE, encoding='utf-8-sig'):
        line = line.strip()
        if line and not line.startswith('#'):
            if not line.startswith('https://'):
                return ('BAD', line)
            return line
    return None


def sync_from_sheet(staff_rows):
    """從 Google 試算表抓人員數，覆寫 staff_rows 的「人員數」欄。
    回傳 (是否有異動, 問題清單)。任何一項檢查沒過就整批不採用。"""
    url = read_sheet_url()
    if url is None:
        return False, []
    if isinstance(url, tuple):
        return False, ['data/staff_sheet_url.txt 的內容不是 https 網址：%s' % url[1]]

    print('讀取 Google 試算表...')
    req = urllib.request.Request(url, headers={'User-Agent': 'tainan-fire-map/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode('utf-8-sig')
    except Exception as e:
        return False, ['讀取 Google 試算表失敗（網址是否設為「發布到網路」且格式選 CSV？）：%s' % e]

    try:
        rows = list(csv.DictReader(io.StringIO(raw)))
    except Exception as e:
        return False, ['Google 試算表內容不是有效的 CSV：%s' % e]

    if not rows:
        return False, ['Google 試算表沒有任何資料列']
    cols = {c.strip() for c in (rows[0].keys() if rows else []) if c}
    for need in ('分隊', '人員數'):
        if need not in cols:
            return False, ['Google 試算表缺少「%s」欄位（目前欄位：%s）'
                           % (need, '、'.join(sorted(cols)) or '無')]

    known = {norm(r['分隊']) for r in staff_rows}
    sheet, problems = {}, []
    for i, r in enumerate(rows, start=2):
        name = norm(r.get('分隊') or '')
        if not name:
            continue
        raw_n = (r.get('人員數') or '').strip().replace(',', '')
        if raw_n == '':
            problems.append('試算表第 %d 列「%s」的人員數是空的' % (i, name))
            continue
        if not raw_n.isdigit():
            problems.append('試算表第 %d 列「%s」的人員數不是數字：%s' % (i, name, raw_n))
            continue
        n = int(raw_n)
        if n <= 0 or n > 999:
            problems.append('試算表第 %d 列「%s」的人員數不合理：%d' % (i, name, n))
            continue
        if name not in known:
            problems.append('試算表有分隊「%s」，但 data/staff.csv 沒有這一隊（名稱打錯？）' % name)
            continue
        if name in sheet:
            problems.append('試算表出現重複的分隊：%s' % name)
            continue
        sheet[name] = n

    missing = sorted(known - set(sheet))
    if missing:
        problems.append('試算表缺少這些分隊：%s' % '、'.join(missing))

    if problems:
        return False, problems

    changed = False
    for r in staff_rows:
        name = norm(r['分隊'])
        if int(r['人員數'] or 0) != sheet[name]:
            print('  %s：%s → %s' % (name, r['人員數'], sheet[name]))
            r['人員數'] = str(sheet[name])
            changed = True
    if changed:
        with io.open(STAFF, 'w', encoding='utf-8', newline='\n') as f:
            w = csv.DictWriter(f, fieldnames=list(staff_rows[0].keys()))
            w.writeheader()
            w.writerows(staff_rows)
        print('  已回寫 data/staff.csv')
    else:
        print('  試算表與 staff.csv 一致')
    return changed, []


def build_fd(pop_by_code):
    vmap = read_csv(VILLAGE_MAP)
    staff_rows = read_csv(STAFF)
    sheet_changed, sheet_problems = sync_from_sheet(staff_rows)

    staff = {norm(r['分隊']): int(r['人員數'] or 0) for r in staff_rows}
    brig = {norm(r['分隊']): norm(r['大隊']) for r in staff_rows}
    note = {norm(r['分隊']): norm(r.get('備註') or '') for r in staff_rows}
    home = {norm(r['分隊']): norm(r.get('無里轄區時歸屬區') or '') for r in staff_rows}

    problems = list(sheet_problems)

    # 對照表的里 vs API 的里
    mapped = {r['里代碼'] for r in vmap}
    got = set(pop_by_code)
    for code in sorted(mapped - got):
        row = next(r for r in vmap if r['里代碼'] == code)
        problems.append('對照表有、戶政司查無此里：%s %s（代碼 %s）' % (row['區'], row['里'], code))
    for code in sorted(got - mapped):
        problems.append('戶政司有新的里、對照表沒有：代碼 %s（%s 人）' % (code, pop_by_code[code]))

    dist_unit = defaultdict(int)       # (區, 分隊) -> 本區人口
    unit_total = defaultdict(int)      # 分隊 -> 全隊人口
    unit_vil = defaultdict(int)        # (區, 分隊) -> 里數
    order = []                         # 區的排列順序 = 對照表出現順序
    for r in vmap:
        d, u, code = norm(r['區']), norm(r['分隊']), r['里代碼']
        if d not in order:
            order.append(d)
        if code not in pop_by_code:
            continue
        p = pop_by_code[code]
        dist_unit[(d, u)] += p
        unit_total[u] += p
        unit_vil[(d, u)] += 1

    for u in sorted(unit_total):
        if u not in staff:
            problems.append('對照表有分隊「%s」，但 data/staff.csv 沒有它的人員數' % u)
        elif staff[u] <= 0:
            problems.append('分隊「%s」人員數是 0，人口比會算不出來' % u)

    FD = {}
    for d in order:
        items = []
        for (dd, u), p in dist_unit.items():
            if dd == d:
                items.append({
                    'name': u,
                    'staff': staff.get(u, 0),
                    'pop': p,
                    'brigade': brig.get(u, ''),
                    'total': unit_total[u],
                    'villages': unit_vil[(d, u)],
                })
        items.sort(key=lambda x: (-x['pop'], x['name']))
        # 沒有里轄區的隊（例如南科分隊）掛在指定的區、排在最後
        for u in sorted(staff):
            if unit_total.get(u, 0) == 0 and home.get(u) == d:
                items.append({'name': u, 'staff': staff[u], 'pop': 0,
                              'brigade': brig.get(u, ''), 'total': 0, 'villages': 0})
        for it in items:
            if note.get(it['name']):
                it['note'] = note[it['name']]
        FD[d] = items

    placed = {u['name'] for us in FD.values() for u in us}
    for u in sorted(staff):
        if u not in placed:
            problems.append('分隊「%s」既沒有里轄區，也沒填「無里轄區時歸屬區」，不會出現在地圖上' % u)

    return FD, problems


# ── 改寫 index.html ───────────────────────────────────────────────────
def rewrite(FD, month, today):
    src = io.open(INDEX, encoding='utf-8').read()
    before = src

    blob = 'const FD=' + json.dumps(FD, ensure_ascii=False, separators=(', ', ': ')) + ';'
    src, n = re.subn(r'^const FD=\{.*\};$', lambda m: blob, src, count=1, flags=re.M)
    assert n == 1, 'index.html 找不到 FD 區塊'

    src, n = re.subn(r'(<span id="dataMonth" data-yyymm=")\d+("[^>]*>)[^<]*(</span>)',
                     lambda m: m.group(1) + str(month) + m.group(2) + roc_label(month) + m.group(3),
                     src, count=1)
    assert n == 1, 'index.html 找不到 dataMonth'

    src, n = re.subn(r'(<span id="updated">)[^<]*(</span>)',
                     lambda m: m.group(1) + today + m.group(2), src, count=1)
    assert n == 1, 'index.html 找不到 updated'

    if src == before:
        return False
    io.open(INDEX, 'w', encoding='utf-8', newline='\n').write(src)
    return True


def current_month():
    src = io.open(INDEX, encoding='utf-8').read()
    m = re.search(r'<span id="dataMonth" data-yyymm="(\d+)"', src)
    assert m, 'index.html 找不到 data-yyymm'
    return int(m.group(1))


def gh_output(**kw):
    p = os.environ.get('GITHUB_OUTPUT')
    if not p:
        return
    with io.open(p, 'a', encoding='utf-8') as f:
        for k, v in kw.items():
            f.write('%s=%s\n' % (k, v))


def main():
    cur = current_month()
    print('目前網站資料月份: %s (%s)' % (cur, roc_label(cur)))
    print('查詢最新可用月份...')
    month, pop = latest_available(cur)

    if not month:
        msg = '戶政司尚未發布 %s 之後的資料（或 API 暫時無回應），本次不更新。' % roc_label(cur)
        print(msg)
        gh_output(changed='false', needs_attention='false')
        io.open(SUMMARY, 'w', encoding='utf-8').write('### 無更新\n\n%s\n' % msg)
        return 0

    FD, problems = build_fd(pop)

    total_pop = sum(u['pop'] for us in FD.values() for u in us)
    units = {u['name'] for us in FD.values() for u in us}
    total_staff = sum(int(r['人員數'] or 0) for r in read_csv(STAFF) if norm(r['分隊']) in units)
    api_total = sum(pop.values())

    if total_pop != api_total:
        problems.append('分配後人口 %s 與戶政司全市 %s 不符，差 %s'
                        % (f'{total_pop:,}', f'{api_total:,}', f'{api_total - total_pop:,}'))

    lines = ['### 臺南市消防分隊服務人口地圖',
             '',
             '| 項目 | 值 |',
             '| --- | --- |',
             '| 資料月份 | %s |' % roc_label(month),
             '| 全市人口 | %s 人 |' % f'{total_pop:,}',
             '| 分隊數 | %d |' % len(units),
             '| 人員合計 | %d |' % total_staff,
             '| 資料來源 | 內政部戶政司開放資料 ODRP013 |',
             '']

    if problems:
        lines += ['### ⚠️ 需要人工處理（本次未更新網站）', '']
        lines += ['- %s' % p for p in problems]
        lines += ['', '請更新 `data/village_map.csv` 或 `data/staff.csv` 後，'
                      '到 Actions 頁面手動重跑一次。']
        io.open(SUMMARY, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
        print('\n'.join('  ! ' + p for p in problems))
        gh_output(changed='false', needs_attention='true',
                  month_label=roc_label(month))
        return 0

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d')
    changed = rewrite(FD, month, today)

    if changed:
        top = sorted(((u['total'] // u['staff'], u['name'], u['staff'], u['total'])
                      for us in FD.values() for u in us if u['staff'] and u['total']),
                     reverse=True)[:5]
        lines += ['### 服務人口比最高的 5 隊', '',
                  '| 分隊 | 人員 | 服務人口 | 人口比 |', '| --- | ---: | ---: | ---: |']
        seen = set()
        for r, u, s, t in top:
            if u in seen:
                continue
            seen.add(u)
            lines.append('| %s | %d | %s | %s 人/員 |' % (u, s, f'{t:,}', f'{r:,}'))
        lines += ['', '網站：https://c85506218-hub.github.io/quiet-pixie-7f902d/']
        print('index.html 已更新 -> %s' % roc_label(month))
    else:
        lines += ['內容與現行版本相同，未產生變更。']
        print('內容無變化')

    io.open(SUMMARY, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    gh_output(changed=str(changed).lower(), needs_attention='false',
              month_label=roc_label(month))
    return 0


if __name__ == '__main__':
    sys.exit(main())
