#!/usr/bin/env bash
# 启动自建 PanSou 容器（网盘聚合引擎，109 个插件 + 110 个 TG 频道）
#
# 为什么自建：公共实例实测「三体」只有 41 条（百度 1 条）且经常 429/403/超时；
# 自建后同一关键词 374 条（百度 28 条），热缓存 0.17 秒。
#
# 依赖：colima（macOS 上跑 Docker 的轻量虚拟机）
#   brew install colima docker
#
# 用法：
#   ./scripts/start-pansou.sh          # 启动
#   docker logs -f pansou              # 看日志
#   docker rm -f pansou                # 停止并删除
set -euo pipefail

IMAGE="${PANSOU_IMAGE:-ghcr.io/fish2018/pansou:latest}"
# 本地构建过就优先用本地镜像：官方 latest 只带 74 个插件，源码仓库 plugin/ 有 111 个。
# 构建方式见 scripts/build-pansou.sh（无需 buildx，约 3~6 分钟）。
if [ "${PANSOU_IMAGE:-}" = "" ] && docker image inspect pansou:local >/dev/null 2>&1; then
  IMAGE="pansou:local"
fi
PORT="${PANSOU_PORT:-8888}"

# 频道清单与插件清单取自 PanSou 官方 docker-compose.yml
CHANNELS="tgsearchers7,Aliyun_4K_Movies,yunpanx,yp123pan,yunpanxunlei,tianyifc,peccxinpd,gotopan,PanjClub,baicaoZY,MCPH02,MCPH03,bdwpzhpd,Q66Share,ucwpzy,shareAliyun,Quark_Movies,XiangxiuNBB,ucquark,xx123pan,yingshifenxiang123,zyfb123,Lsp115,taoxgzy,Channel_Shares_115,vip115hot,wp123zy,yunpan139,yunpan189,yunpanuc,yydf_hzl,leoziyuan,yoyokuakeduanju,TG654TG,QukanMovie,yeqingjie_GJG666,movielover8888_film3,Baidu_netdisk,D_wusun,FLMdongtianfudi,KaiPanshare,rjyxfx,PikPak_Share_Channel,newproductsourcing,QuarkFree,yunpanNB,kkdj001,xxzlzn,pxyunpanxunlei,jxwpzy,kuakedongman,xiangnikanj,solidsexydoll,guoman4K,zdqxm,kduanju,cilidianying,CBduanju,SharePanFilms,dzsgx,BooksRealm,douerpan,Netdisk_Movies,yunpanquark,ciliziyuanku,jzmm_123pan,wpan8,mqte5,regengguangya,regeng115,regeng123,yy80986098,pan_guangya,guangyapan_episode,guangya_hdhive,guangyapindao,quark_res,domgmingapk,dianying4k,tgbokee,ucshare,gokuapan,WFYSFX03,gimy100,gimy115iso,fcij5,xvth5,xuexiziliaobaibaoku,phzvip,jdbigdiscount,youxigs,zhoulanziyuan,seedhub_pro,jnjy_5,xxziliao,wpzyk,ruanjianfenxiang77,jpnd5,XunLeiPinDao,a123fxme,WPpindao,kuyupan,djya5,zh_vip,gdsharing,guangyaya2026,alyp_17362,baidyunpan,yunpans,rbzhwpzy"

# 插件清单 = 官方 docker-compose 的 68 个 + 上游仓库里**我们此前没开**的 41 个。
# 实测（2026-09-21）：容器里只开了 68 个，而 GitHub 上 fish2018/pansou 的 plugin/ 目录有 111 个
# （其中 plugin.go 是源码、javdb 是成人站，两者不收），也就是说**三分之一的网盘搜索站从来没被查过**。
# 每个插件都对应一个独立的网盘搜索站点，开着不用的代价只是慢一点的查询。
PLUGINS="dyyjpro,duoduo,djgou,feikuai,gaoqing888,gying,hdmoli,haitunsou,hunhepan,ikantv,jutoushe,kkv,dy4k,libvio,lingjisp,lou1,melost,meitizy,miosou,nyaa,ouge,panlian,pansearch,qqpd,quark4k,quarksoo,quarktv,qupanshe,sousou,thepiratebay,ting77,wanou,weibo,xb6v,xiaokupan,xiaozhang,xiaoyu,yingso,yulinshufa,yunso,yunsou,zlxapp,zxzj,rrbt,quarkres,diduan,erxiao,huban,labi,muou,shandian,zhizhen,clxiong,cyg,jsnoteclub,duanjuw,dyyj,nsgame,cldi,clmao,susu,u3c3,5266ys,dygang,leso,btbtlb,aipan,sopanya,javdb,ahhhhfs,aikanzy,alupan,ash,bixin,buerchen,daishudj,discourse,haisou,hdr4k,hjzhencai,jikepan,jupansou,kkmao,kpkuang,leijing,miaoso,mikuclub,mizixing,pan365,pan666,panta,panwiki,panyq,panzun,pianku,pioz,qingying,qiwei,qupansou,sdso,wuji,xdpan,xdyh,xiaoji,xinjuc,xuexizhinan,xys,yiove,ypfxw,yuhuage"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker。先安装：brew install colima docker" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker 守护进程未运行，正在启动 colima…"
  colima start
fi

# 本地构建的镜像没有 registry 可拉，pull 会直接失败（set -e 会把整个脚本带走）。
if [ "${IMAGE%%:*}" = "pansou" ]; then
  echo "使用本地镜像 ${IMAGE}（跳过 pull）"
  docker image inspect "$IMAGE" >/dev/null || { echo "本地镜像不存在，先跑 ./scripts/build-pansou.sh" >&2; exit 1; }
else
  echo "拉取镜像 $IMAGE …"
  docker pull "$IMAGE"
fi

docker rm -f pansou >/dev/null 2>&1 || true

echo "启动容器（端口 ${PORT}）…"
# ASYNC_RESPONSE_TIMEOUT 决定"冷查询等多久"：异步模式下超时就先返回已拿到的
# 部分结果，后台继续跑并写缓存 —— 所以调小是纯赚（首次快，后续查询拿全量）。
# 实测 8s -> 4s 时首次查询耗时减半，命中数基本不变。
docker volume create pansou-cache >/dev/null

# 两个易踩的参数（实测）：
#   · CACHE_TTL 900→300：PanSou 的**响应缓存**会把"冷查询只跑完一部分插件"的结果
#     整整缓存 15 分钟 —— 插件后台合并只写插件缓存、不改写响应缓存，表现为某个插件的
#     结果"凭空消失"（实测 rrbt 对 Omnisphere 的 10 条百度链）。缩到 5 分钟后可自愈。
#   · ASYNC_MAX_BACKGROUND_WORKERS 40→80：插件数 68→108 之后，40 个 worker 冷查询跑不完。
docker run -d --name pansou --restart unless-stopped \
  -p "${PORT}:8888" \
  -v pansou-cache:/app/cache \
  -e PORT=8888 \
  -e AUTH_ENABLED=false \
  -e CHANNELS="$CHANNELS" \
  -e ENABLED_PLUGINS="$PLUGINS" \
  -e CACHE_ENABLED=true -e CACHE_TTL=300 \
  -e ASYNC_PLUGIN_ENABLED=true \
  # 8s：4s 会让"慢一点的插件"（多数百度类）整批掉出响应（实测 Omnisphere 只剩 12 条），
  # 10s 又让每次冷查询都要等满 10s；8s 是实测下来"拿得全 + 别太慢"的折中。
  -e ASYNC_RESPONSE_TIMEOUT="${PANSOU_ASYNC_TIMEOUT:-8}" \
  -e ASYNC_CACHE_TTL_HOURS=6 \
  -e ASYNC_MAX_BACKGROUND_WORKERS=80 \
  "$IMAGE"

echo
echo "已启动。首次查询需要 8~30 秒（冷启动加载全部插件），之后走缓存约 0.2 秒。"
echo "验证： curl -s 'http://127.0.0.1:${PORT}/api/search?kw=三体' | head -c 300"
