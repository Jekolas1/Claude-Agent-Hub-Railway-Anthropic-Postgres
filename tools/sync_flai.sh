#!/bin/bash
# Upload the existing agents from ~/flai-work to the Claude Agent Hub on Railway.
# Needs only what ships with macOS (bash, curl, perl). Changes nothing in flai-work.
# Safe to re-run: refreshes the cloud copy from the local files.
#   Usage:  bash ~/flai-work/sync_to_cloud.sh
set -euo pipefail

HUB="${HUB_URL:-https://claude-agent-hub-production.up.railway.app}"
ROOT="${FLAI_WORK:-$HOME/flai-work}"
# agent | tools (comma separated) | may write shared knowledge | names on WhatsApp (comma separated)
AGENTS="gershon||true|גרשון
gefen|web_search|true|גפן
tirosh||false|תירוש"
MAX=30000

[ -d "$ROOT" ] || { echo "Folder not found: $ROOT"; exit 1; }
if [ -z "${HUB_TOKEN:-}" ]; then read -r -s -p "HUB_TOKEN (hidden): " HUB_TOKEN; echo; fi
RESP="$(mktemp)"; trap 'rm -f "$RESP"' EXIT
SKIPPED=""

put() {  # put <path> ; JSON body on stdin
  local code
  code=$(curl -s -o "$RESP" -w '%{http_code}' -X PUT "$HUB$1" \
    -H "Authorization: Bearer $HUB_TOKEN" -H 'Content-Type: application/json' --data-binary @-)
  if [ "$code" != 200 ]; then echo "✗ PUT $1 -> $code $(head -c 300 "$RESP")"; exit 1; fi
}

json_value() {  # {"value": <file content>}
  perl -MJSON::PP -e 'open my $f,"<",$ARGV[0] or die "$ARGV[0]: $!"; local $/; my $s=<$f>; utf8::decode($s);
    print JSON::PP->new->ascii->encode({value=>$s})' "$1"
}

json_agent() {  # {"instructions": <SKILL.md>, "tools": [...], "shared_write": bool, "names": [...]}
  perl -MJSON::PP -e 'open my $f,"<",$ARGV[0] or die "$ARGV[0]: $!"; local $/; my $s=<$f>; utf8::decode($s);
    my $n=$ARGV[3]; utf8::decode($n);
    print JSON::PP->new->ascii->encode({instructions=>$s, tools=>[grep {length} split /,/, $ARGV[1]],
      shared_write=>($ARGV[2] eq "true" ? JSON::PP::true : JSON::PP::false), names=>[grep {length} split /,/, $n]})' "$1" "$2" "$3" "$4"
}

urlpath() { perl -pe 's/([^A-Za-z0-9\/._~-])/sprintf("%%%02X",ord($1))/ge' <<<"$1"; }

upload() {  # upload <agent_id> <file>
  local key="${2#"$ROOT"/}" chars
  chars=$(perl -e 'open my $f,"<",$ARGV[0]; local $/; my $s=<$f>; utf8::decode($s); $s=~s/\s//g; print length($s)' "$2")
  [ "$chars" -gt 0 ] || return 0
  if [ "$(perl -e 'open my $f,"<",$ARGV[0]; local $/; my $s=<$f>; utf8::decode($s); print length($s)' "$2")" -gt $MAX ]; then
    SKIPPED+="   - $key (too long)"$'\n'; return 0
  fi
  json_value "$2" | put "/agents/$1/knowledge/$(urlpath "$key")"
  echo "   + $key"
}

while IFS='|' read -r agent tools shared names; do
  dir="$ROOT/Agents/$agent"
  skill="$dir/plugin/skills/$agent/SKILL.md"
  if [ ! -f "$skill" ]; then echo "! $agent: no SKILL.md, skipped"; continue; fi
  json_agent "$skill" "$tools" "$shared" "$names" | put "/agents/$agent"
  echo "✓ $agent ($names, tools=[$tools], shared_write=$shared)"
  while IFS= read -r -d '' f; do
    case "$f" in
      *.md|*.txt|*.csv) upload "$agent" "$f" ;;
      *) SKIPPED+="   - ${f#"$ROOT"/} (not uploaded yet)"$'\n' ;;
    esac
  done < <(find "$dir" -type f ! -name '.DS_Store' ! -path '*/plugin/*' -print0 | sort -z)
done <<<"$AGENTS"

echo "✓ shared"
for d in clients shared; do
  [ -d "$ROOT/$d" ] || continue
  while IFS= read -r -d '' f; do upload "_shared" "$f"; done \
    < <(find "$ROOT/$d" -type f \( -name '*.md' -o -name '*.txt' -o -name '*.csv' \) -print0 | sort -z)
done

[ -n "$SKIPPED" ] && printf '\nNot uploaded (images and the like come in a later stage):\n%s' "$SKIPPED"
echo
curl -s "$HUB/agents" -H "Authorization: Bearer $HUB_TOKEN"; echo
echo "Done."
