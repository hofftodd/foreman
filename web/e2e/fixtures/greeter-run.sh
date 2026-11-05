# A tiny test runner for the screenshot scene: every tests/check_*.sh is a test.
# Writes JUnit XML and LCOV into $FOREMAN_RESULTS like a real runner would; each line
# of the project's *.sh files counts as covered unless it is marked "# uncovered".
out="${FOREMAN_RESULTS:-/tmp}"
pass=0; fail=0; cases=""
for t in tests/check_*.sh; do
  name=$(basename "$t" .sh)
  start=$(date +%s%N)
  if sh "$t" >/dev/null 2>&1; then status=""; pass=$((pass+1))
  else status="<failure message=\"$name: unexpected output\"/>"; fail=$((fail+1)); fi
  ms=$(( ($(date +%s%N) - start) / 1000000 ))
  cases="$cases<testcase classname=\"tests.$name\" name=\"$name\" file=\"$t\" time=\"0.$(printf %03d "$ms")\">$status</testcase>"
done
echo "<testsuite tests=\"$((pass+fail))\">$cases</testsuite>" > "$out/junit.xml"
: > "$out/lcov.info"
for f in *.sh; do
  [ -f "$f" ] || continue
  { echo "SF:$PWD/$f"; i=0
    while IFS= read -r line; do i=$((i+1))
      case "$line" in *"# uncovered"*) echo "DA:$i,0";; *) echo "DA:$i,3";; esac
    done < "$f"; echo end_of_record; } >> "$out/lcov.info"
done
[ "$fail" -eq 0 ]
