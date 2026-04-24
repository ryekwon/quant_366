import requests
codes = ['513310', '513100', '159941', '518880', '510050', '159985', '159100']
url = 'https://push2.eastmoney.com/api/qt/ulist.np/get'
params = {
    'fltt': 2,
    'invt': 2,
    'fields': 'f12,f13,f14,f57,f58',
    'secids': ','.join(['1.' + c if c.startswith('5') else '0.' + c for c in codes]),
    '_': '1234567890'
}
r = requests.get(url, params=params, timeout=5)
data = r.json()
items = data.get('data', {}).get('diff', [])
for item in items:
    print(item.get('f12'), '|', item.get('f14'), '|', item.get('f57'), '|', item.get('f58'))
