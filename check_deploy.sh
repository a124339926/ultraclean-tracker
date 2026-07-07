#!/bin/bash
# UltraClean Tracker 部署檢查腳本
# 用法：bash check_deploy.sh [RENDER_URL]

RENDER_URL="${1:-https://ultraclean-tracker.onrender.com}"
FRONTEND_URL="https://a124339926.github.io/ultraclean-tracker/"

echo "========================================="
echo "  UltraClean Tracker 部署檢查"
echo "========================================="
echo ""

# 1. 檢查前端
echo "[1/4] 檢查前端 GitHub Pages..."
FRONTEND_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$FRONTEND_URL")
if [ "$FRONTEND_HTTP" = "200" ]; then
  echo "  ✓ 前端正常 (HTTP $FRONTEND_HTTP)"
else
  echo "  ✗ 前端異常 (HTTP $FRONTEND_HTTP) - 稍等1~2分鐘後重試"
fi

# 2. 檢查後端存活
echo "[2/4] 檢查後端存活..."
BACKEND_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$RENDER_URL/api/config" --connect-timeout 10)
if [ "$BACKEND_HTTP" = "200" ]; then
  echo "  ✓ 後端正常 (HTTP $BACKEND_HTTP)"
else
  echo "  ✗ 後端無法連線 (HTTP $BACKEND_HTTP)"
  echo "    提示：Render 首次部署需要 5~10 分鐘，請稍後重試"
fi

# 3. 檢查 API 功能
echo "[3/4] 檢查 API 功能..."
API_RESPONSE=$(curl -s "$RENDER_URL/api/config" --connect-timeout 10)
if echo "$API_RESPONSE" | grep -q "categories"; then
  echo "  ✓ API 回應正常"
  echo "  業務線數量: $(echo "$API_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('categories','')))" 2>/dev/null || echo 'N/A')"
else
  echo "  ✗ API 回應異常"
fi

# 4. 檢查專案數據
echo "[4/4] 檢查專案數據..."
PROJECTS=$(curl -s "$RENDER_URL/api/projects?limit=1" --connect-timeout 10)
PROJECT_COUNT=$(echo "$PROJECTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total',0))" 2>/dev/null || echo '0')
if [ "$PROJECT_COUNT" -gt 0 ]; then
  echo "  ✓ 數據正常 - 共 $PROJECT_COUNT 筆專案"
else
  echo "  ⚠ 暫無數據（需先匯入 video_tracker.db）"
fi

echo ""
echo "========================================="
echo "  前端: $FRONTEND_URL"
echo "  後端: $RENDER_URL"
echo "========================================="
