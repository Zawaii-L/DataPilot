import pandas as pd
from time_table_tools import generate_hourly_time_table

df = pd.DataFrame({
    "valid":["2026-09-17 00:10","2026-09-17 00:40","2026-09-17 01:10"],
    "tmpc":[28.1,28.3,27.9],
    "relh":[80,82,85],
    "p01i":[0,0,1],
    "sknt":[5,6,4]
})

result = generate_hourly_time_table(df)
print(result)

assert len(result)==2
assert "平均气温" in result.columns
assert "平均湿度" in result.columns
assert "小时降水" in result.columns
assert "平均风速" in result.columns

print("DataPilot Time Table Tools Regression PASS")
