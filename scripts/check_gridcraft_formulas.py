"""Synthetic XLSX regression: blank inputs, formula results, SUM and round-trip."""
import argparse
import json
from pathlib import Path
import subprocess
import zipfile


def fixture(path):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    data = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets><sheet name="Blank inputs" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': f'''<worksheet xmlns="{ns}"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t></t></is></c><c r="B1"><v>120</v></c><c r="C1"><f>IF(A1=0,"",B1/A1)</f></c><c r="D1" t="str"><f>IF(1=1,"","x")</f><v></v></c><c r="E1" t="inlineStr"><is><t>00123</t></is></c><c r="F1" t="inlineStr"><is><t xml:space="preserve"> </t></is></c><c r="G1" t="inlineStr"><is><t>中文合计</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is/></c><c r="B2"><v>80</v></c><c r="C2"><f>IF(A2=0,"",B2/A2)</f></c></row>
<row r="3"><c r="A3" t="inlineStr"><v></v></c><c r="B3"><f>SUM(B1:B2)</f></c><c r="C3"><f>IF(A3=0,"",B1/A3)</f></c></row>
<row r="4"><c r="A4"><v>2</v></c><c r="C4"><f>IF(A4=0,"",B1/A4)</f></c></row>
</sheetData></worksheet>''',
    }
    data['[Content_Types].xml'] = data['[Content_Types].xml'].replace('</Types>', '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    data['xl/_rels/workbook.xml.rels'] = data['xl/_rels/workbook.xml.rels'].replace('</Relationships>', f'<Relationship Id="rId2" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
    data['xl/styles.xml'] = f'<styleSheet xmlns="{ns}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in data.items():
            z.writestr(name, text)


def calls(cli, steps):
    messages = [{'jsonrpc':'2.0', 'id':i, 'method':'tools/call', 'params':{'name':name, 'arguments':args}} for i,(name,args) in enumerate(steps)]
    proc = subprocess.run([str(cli), 'mcp'], input=''.join(json.dumps(m)+'\n' for m in messages), capture_output=True, encoding='utf-8', timeout=120, check=True)
    rows = {row['id']:row for row in map(json.loads, proc.stdout.splitlines())}
    results=[]
    for i in range(len(steps)):
        row=rows[i]
        assert 'error' not in row and not row['result'].get('isError'), row
        results.append(json.loads(row['result']['content'][0]['text']))
    return results


def inspect(cli, path, saved=None):
    cells = ['A1','A2','A3','C1','C2','C3','C4','B3','D1','E1','F1','G1']
    steps=[('open_workbook',{'path':str(path)})] + [('get_cell',{'cell':cell}) for cell in cells]
    steps += [('select',{'range':'B1:B2'}),('execute_command',{'command':'selection.stats'})]
    if saved:
        steps += [('save_workbook',{'path':str(saved)})]
    rows=calls(cli,steps)
    return {'cells':dict(zip(cells,rows[1:1+len(cells)])), 'stats':rows[2+len(cells)]}


def check(result):
    expected={'A1':None,'A2':None,'A3':None,'C1':'','C2':'','C3':'','C4':60.0,'B3':200.0,'D1':'','E1':'00123','F1':' ','G1':'中文合计'}
    for cell,value in expected.items():
        assert result['cells'][cell]['value']==value, (cell,result['cells'][cell])
    assert result['stats']['sum']==200 and result['stats']['average']==100 and result['stats']['numeric']==2


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cli',type=Path,required=True)
    p.add_argument('--baseline',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    original=args.output/'blank-inputs.xlsx';saved=args.output/'roundtrip.xlsx'
    fixture(original)
    report={}
    if args.baseline:
        report['baseline']=inspect(args.baseline,original)
        assert report['baseline']['cells']['C1']['value'] != '', 'baseline did not reproduce'
    report['fixed']=inspect(args.cli,original,saved);check(report['fixed'])
    report['fresh_process_reopen']=inspect(args.cli,saved);check(report['fresh_process_reopen'])
    (args.output/'formula-regression.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: empty inline inputs, IF guards, SUM, selection statistics, text preservation, save and fresh-process reopen')
