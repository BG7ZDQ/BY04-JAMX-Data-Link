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
中的平均轨道根数，校验轨道有效、滤波器收敛、时间连续且数据不超过 48 小时后，
生成带标准校验和的 BY04 与 JAMX01 TLE。`latest.tle` 保存最新结果，
`history.tle` 按遥测历元去重追加历史结果；更新完成后会自动重新发布 GitHub Pages。

也可以在 Actions 页面手动运行 **Update ephemeris**。本地验证与离线回放命令：

```bash
python -m unittest discover -s tests -v
python scripts/build_ephemeris.py --input tests/telemetry_fixture.json --allow-stale --dry-run
```
