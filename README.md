# BY04 & JAMX Data Link

BY04 与 JAMX 是分别由哈尔滨工业大学附属中学、静安区青少年活动交流中心承接的青少年科普卫星项目。

详请参见：
- [哈尔滨号百科](https://sat.huijiwiki.com/wiki/%E5%93%88%E5%B0%94%E6%BB%A8%E5%8F%B7)

本仓库对这两颗卫星的部分数据与资源做出了汇总，包括了：
- [快捷导航页面](https://8104.satellites.ac.cn/index.html)
- [历史星历数据](https://8104.satellites.ac.cn/history.tle)
- [最新星历数据](https://8104.satellites.ac.cn/latest.tle)

## 星历自动更新

仓库每天北京时间 08:17 通过 GitHub Actions 读取遥测服务器 `BY-04/GNS-S1`
中的 GNSS 平均轨道根数和 ECEF 状态量，校验轨道有效、滤波器收敛、时间连续且
数据不超过 48 小时后，将状态量转换到 TEME 并反拟合 SGP4 均值，生成带标准
校验和的 BY04 与 JAMX01 TLE。量化后的历元残差超过 1 km 或 1 m/s 时任务会失败，
新旧 TLE 在新历元处的位置跳变超过 100 km 或速度跳变超过 150 m/s 时也会失败，
不会发布异常星历或历元倒退的数据。`latest.tle` 保存最新结果，
`history.tle` 按遥测历元去重追加历史结果；更新完成后会自动重新发布 GitHub Pages。

也可以在 Actions 页面手动运行 **Update ephemeris**。本地验证与离线回放命令：

```bash
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests -v
python scripts/build_ephemeris.py --input tests/telemetry_fixture.json --allow-stale --dry-run
```
