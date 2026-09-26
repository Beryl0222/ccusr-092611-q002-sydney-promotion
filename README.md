# 悉尼文旅推介协作库

## 项目愿景

悉尼中国文化中心把江苏十三市的影像、非遗作品和活动说明交给多家合作机构使用。协作库要在多方参与、译稿先后到达、合作方偶发离线的情况下,始终回答清楚四个问题:**谁能改什么、哪些内容已获授权、哪些修改还在等对方确认、每个城市每种语言最终发出了什么**。所有写入走 SQLite 事务,状态变化全部留痕,任何一次发布都可以被对方用摘要独立核验。

## 目录

- src/sydney_promotion/domain.py:领域对象、江苏十三市与语言常量、时间约定。
- src/sydney_promotion/service.py:事务、状态迁移、权限、版本与幂等边界。
- src/sydney_promotion/api.py:本地 HTTP 接口(身份取自 `X-Org-Id` 请求头)。
- tests/:状态、版本、权限、幂等、断线恢复与清单核验测试。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests

## 启动本地服务

    PYTHONPATH=src python3 -c "from sydney_promotion.api import serve; serve()"

默认使用文件库 `sydney_promotion.db`,重启后记录与交接进度不丢失。
