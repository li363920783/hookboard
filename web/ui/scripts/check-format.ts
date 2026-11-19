// mdInline feeds dangerouslySetInnerHTML: pin the escaping and the order of
// the bold / code-span passes.  Run with `pnpm check`.
import assert from "node:assert/strict";
import { mdInline } from "../src/lib/format";

const cases: [string, string][] = [
  ["**单据**：bug 1", "<strong>单据</strong>：bug 1"],
  ["`GET https://api.tapd.cn/bugs?id=1`", "<code>GET https://api.tapd.cn/bugs?id=1</code>"],
  // Bold inside a code span stays literal code.
  ["`a **b** c`", "<code>a **b** c</code>"],
  // Ticket text never becomes markup.
  ["<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"],
  ["`<img src=x onerror=1>`", "<code>&lt;img src=x onerror=1&gt;</code>"],
  ["& < >", "&amp; &lt; &gt;"],
  // Unbalanced backticks stay literal; stray NULs cannot become placeholders.
  ["`a", "`a"],
  ["a\u00000\u0000b", "a0b"],
];

for (const [input, expected] of cases) {
  assert.equal(mdInline(input), expected, `mdInline(${JSON.stringify(input)})`);
}
console.log(`mdInline: ${cases.length} cases ok`);
