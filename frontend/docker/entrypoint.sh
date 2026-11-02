#!/bin/sh
# 启动 CabinWeave 前端；浏览器通过同源路径访问后端。
set -eu
exec nginx -g 'daemon off;'
