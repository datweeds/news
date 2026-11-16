#!/bin/sh
# News status: backlog, stories, top ranking, memory. Run on the server.
cd ~/news || exit 1
docker compose exec -T db psql -U news -d news -tA \
  -c "SELECT 'items unembedded/total: ' || count(*) FILTER (WHERE embedding IS NULL AND duplicate_of IS NULL) || '/' || count(*) FROM items" \
  -c "SELECT 'active stories: ' || count(*) || ', multi-item: ' || count(*) FILTER (WHERE item_count > 1) || ', biggest: ' || max(item_count) FROM clusters WHERE status = 'active'" \
  -c "SELECT 'enriched: ' || count(*) FROM items WHERE enrichment IS NOT NULL"
K=$(cat config/.admin-key)
curl -s -H "Authorization: Bearer $K" "localhost:3012/v1/admin/clusters?limit=${1:-12}" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f\"{c['score']:5.1f} {c['items']:3} items {c['sources']:2} src  {c['title'][:85]}\")"
docker stats --no-stream --format "{{.Name}} {{.MemUsage}}" | grep news
