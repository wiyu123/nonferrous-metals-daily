#!/usr/bin/env python3
with open('metal_monitor.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Restore base64 images
old = r"""    if comparison_b64_list_a:
        comparison_section += (
            '<h3 style="color:#2c3e50;border-left:4px solid #3498db;padding-left:10px">'
            '\U0001f4c8 A组：期货品种走势对比</h3>'
            '<p style="font-size:12px;color:#888">（共 %s 张走势对比图，详见附件）</p>'
        ) % len(comparison_b64_list_a)

    if comparison_b64_list_b:
        comparison_section += (
            '<h3 style="color:#2c3e50;border-left:4px solid #E67E22;padding-left:10px">'
            '\U0001f4c8 B组：稀缺小金属走势对比</h3>'
            '<p style="font-size:12px;color:#888">（共 %s 张走势对比图，详见附件）</p>'
        ) % len(comparison_b64_list_b)"""

new = """    if comparison_b64_list_a:
        comparison_section += '<h3 style="color:#2c3e50;border-left:4px solid #3498db;padding-left:10px">\\U0001f4c8 A组：期货品种走势对比（基准日=100）</h3>'
        for idx, b64 in enumerate(comparison_b64_list_a):
            label = '[%s/%s]' % (idx+1, len(comparison_b64_list_a))
            comparison_section += '<p style="font-size:12px;color:#888;margin:5px 0 2px 0">%s</p>' % label
            comparison_section += '<img src="data:image/png;base64,%s" style="max-width:100%%;border:1px solid #ddd;border-radius:4px;margin:5px 0 10px 0;box-shadow:0 2px 8px rgba(0,0,0,0.1)" alt="A组走势对比图" />' % b64

    if comparison_b64_list_b:
        comparison_section += '<h3 style="color:#2c3e50;border-left:4px solid #E67E22;padding-left:10px">\\U0001f4c8 B组：稀缺小金属走势对比（基准日=100）</h3>'
        for idx, b64 in enumerate(comparison_b64_list_b):
            label = '[%s/%s]' % (idx+1, len(comparison_b64_list_b))
            comparison_section += '<p style="font-size:12px;color:#888;margin:5px 0 2px 0">%s</p>' % label
            comparison_section += '<img src="data:image/png;base64,%s" style="max-width:100%%;border:1px solid #ddd;border-radius:4px;margin:5px 0 10px 0;box-shadow:0 2px 8px rgba(0,0,0,0.1)" alt="B组走势对比图" />' % b64"""

assert old in code, "OLD IMG BLOCK NOT FOUND"
code = code.replace(old, new)
print("1. base64 images restored")

# 2. Remove 数据截至 column from table rows & header
old_row_end = """+ color_span(chg_long) +
            '<td style="text-align:left;font-size:10px;color:#555;padding:6px 8px;line-height:1.4">' + inv + '</td>'
            '<td style="text-align:left;font-size:10px;color:#555;padding:6px 8px;line-height:1.4">' + sd + '</td>'
            '<td style="text-align:center;font-size:10px;color:#666;padding:8px;white-space:nowrap">' + latest['date'] + '</td>'
            '</tr>'"""

new_row_end = """+ color_span(chg_long) +
            '<td style="text-align:left;font-size:10px;color:#555;padding:6px 8px;line-height:1.4">' + inv + '</td>'
            '<td style="text-align:left;font-size:10px;color:#555;padding:6px 10px;line-height:1.4">' + sd + '</td>'
            '</tr>'"""

if old_row_end in code:
    code = code.replace(old_row_end, new_row_end)
    print("2a. Date column removed from rows")
else:
    print("2a. WARNING: row end not found")

old_header_end = """<th style="padding:8px;border:1px solid #2c3e50;width:4%">品种</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:9%">参考价格</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">5日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">' + str(short_period) + '日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">' + str(long_period) + '日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:35%">行业库存</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:30%">供需简析</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:8%">数据截至</th>'"""

new_header_end = """<th style="padding:8px;border:1px solid #2c3e50;width:4%">品种</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:10%">参考价格</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">5日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">' + str(short_period) + '日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">' + str(long_period) + '日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:33%">行业库存</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:38%">供需简析</th>'"""

if old_header_end in code:
    code = code.replace(old_header_end, new_header_end)
    print("2b. Date column removed from header, widths adjusted")
else:
    print("2b. WARNING: header end not found")

# 3. Add date info to table titles in build_email_html
old_title_a = "'📋 A组：传统有色金属'"
new_title_a = "'📋 A组：传统有色金属  数据截至：' + (futures_metals[0]['latest']['date'] if futures_metals else now_str)"
if old_title_a in code:
    code = code.replace(old_title_a, new_title_a, 1)  # only first occurrence
    print("3a. A组 title with date")
else:
    print("3a. WARNING: A title not found")

old_title_b = "'📋 B组：稀缺小金属'"
new_title_b = "'📋 B组：稀缺小金属  数据截至：' + (small_metals[0]['latest']['date'] if small_metals else now_str)"
if old_title_b in code:
    code = code.replace(old_title_b, new_title_b, 1)
    print("3b. B组 title with date")
else:
    print("3b. WARNING: B title not found")

with open('metal_monitor.py', 'w', encoding='utf-8') as f:
    f.write(code)

import py_compile
py_compile.compile('metal_monitor.py', doraise=True)
print("All done, Syntax OK")
