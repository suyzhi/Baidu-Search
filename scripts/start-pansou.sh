#!/usr/bin/env bash
# 启动自建 PanSou 容器（网盘聚合引擎，48+ 插件 + 100+ TG 频道）
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

IMAGE="ghcr.io/fish2018/pansou:latest"
PORT="${PANSOU_PORT:-8888}"

# 频道清单与插件清单取自 PanSou 官方 docker-compose.yml
CHANNELS="tgsearchers7,Aliyun_4K_Movies,yunpanx,yp123pan,yunpanxunlei,tianyifc,peccxinpd,gotopan,PanjClub,baicaoZY,MCPH02,MCPH03,bdwpzhpd,Q66Share,ucwpzy,shareAliyun,Quark_Movies,XiangxiuNBB,ucquark,xx123pan,yingshifenxiang123,zyfb123,Lsp115,taoxgzy,Channel_Shares_115,vip115hot,wp123zy,yunpan139,yunpan189,yunpanuc,yydf_hzl,leoziyuan,yoyokuakeduanju,TG654TG,QukanMovie,yeqingjie_GJG666,movielover8888_film3,Baidu_netdisk,D_wusun,FLMdongtianfudi,KaiPanshare,rjyxfx,PikPak_Share_Channel,newproductsourcing,QuarkFree,yunpanNB,kkdj001,xxzlzn,pxyunpanxunlei,jxwpzy,kuakedongman,xiangnikanj,solidsexydoll,guoman4K,zdqxm,kduanju,cilidianying,CBduanju,SharePanFilms,dzsgx,BooksRealm,douerpan,Netdisk_Movies,yunpanquark,ciliziyuanku,jzmm_123pan,wpan8,mqte5,regengguangya,regeng115,regeng123,yy80986098,pan_guangya,guangyapan_episode,guangya_hdhive,guangyapindao,quark_res,domgmingapk,dianying4k,tgbokee,ucshare,gokuapan,WFYSFX03,gimy100,gimy115iso,fcij5,xvth5,xuexiziliaobaibaoku,phzvip,jdbigdiscount,youxigs,zhoulanziyuan,seedhub_pro,jnjy_5,xxziliao,wpzyk,ruanjianfenxiang77,jpnd5,XunLeiPinDao,a123fxme,WPpindao,kuyupan,djya5,zh_vip,gdsharing,guangyaya2026,alyp_17362,baidyunpan,yunpans,rbzhwpzy"

PLUGINS="dyyjpro,duoduo,djgou,feikuai,gaoqing888,gying,hdmoli,haitunsou,hunhepan,ikantv,jutoushe,kkv,dy4k,libvio,lingjisp,lou1,melost,meitizy,miosou,nyaa,ouge,panlian,pansearch,qqpd,quark4k,quarksoo,quarktv,qupanshe,sousou,thepiratebay,ting77,wanou,weibo,xb6v,xiaokupan,xiaozhang,xiaoyu,yingso,yulinshufa,yunso,yunsou,zlxapp,zxzj,rrbt,quarkres,diduan,erxiao,huban,labi,muou,shandian,zhizhen,clxiong,cyg,jsnoteclub,duanjuw,dyyj,nsgame,cldi,clmao,susu,u3c3,5266ys,dygang,leso,btbtlb,aipan,sopanya"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker。先安装：brew install colima docker" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker 守护进程未运行，正在启动 colima…"
  colima start
fi

echo "拉取镜像 $IMAGE …"
docker pull "$IMAGE"

docker rm -f pansou >/dev/null 2>&1 || true

echo "启动容器（端口 $PORT）…"
# ASYNC_RESPONSE_TIMEOUT 决定"冷查询等多久"：异步模式下超时就先返回已拿到的
# 部分结果，后台继续跑并写缓存 —— 所以调小是纯赚（首次快，后续查询拿全量）。
# 实测 8s -> 4s 时首次查询耗时减半，命中数基本不变。
docker volume create pansou-cache >/dev/null

docker run -d --name pansou --restart unless-stopped \
  -p "${PORT}:8888" \
  -v pansou-cache:/app/cache \
  -e PORT=8888 \
  -e CHANNELS="$CHANNELS" \
  -e ENABLED_PLUGINS="$PLUGINS" \
  -e CACHE_ENABLED=true -e CACHE_TTL=900 \
  -e ASYNC_PLUGIN_ENABLED=true \
  -e ASYNC_RESPONSE_TIMEOUT=4 \
  -e ASYNC_CACHE_TTL_HOURS=6 \
  -e ASYNC_MAX_BACKGROUND_WORKERS=40 \
  "$IMAGE"

echo
echo "已启动。首次查询需要 8~30 秒（冷启动加载全部插件），之后走缓存约 0.2 秒。"
echo "验证： curl -s 'http://127.0.0.1:${PORT}/api/search?kw=三体' | head -c 300"
