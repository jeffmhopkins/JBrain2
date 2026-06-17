// The article body renderer: a small typed-block walk (paragraphs, bulleted
// lists, simple tables) with inline `[n]` citations. Intentionally not a
// markdown engine — the wiki emits a bounded block model, so the reader stays
// contained and testable (docs/mocks/wiki-reader-example-priya.html).

import type { WikiBlock } from "../../api/client";
import { withCitations } from "./citations";

function Block({ block, onCite }: { block: WikiBlock; onCite: (n: number) => void }) {
  if (block.kind === "p") {
    return <p className="wiki-p">{withCitations(block.text, onCite)}</p>;
  }
  if (block.kind === "ul") {
    return (
      <ul className="wiki-list">
        {block.items.map((item, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: list items are static per article.
          <li key={i}>{withCitations(item, onCite)}</li>
        ))}
      </ul>
    );
  }
  return (
    <table className="wiki-table">
      <thead>
        <tr>
          {block.header.map((cell, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: header cells are static per article.
            <th key={i}>{withCitations(cell, onCite)}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {block.rows.map((row, r) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: rows are static per article.
          <tr key={r}>
            {row.map((cell, c) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: cells are static per article.
              <td key={c}>{withCitations(cell, onCite)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ArticleBody({
  blocks,
  onCite,
}: {
  blocks: WikiBlock[];
  onCite: (n: number) => void;
}) {
  return (
    <>
      {blocks.map((block, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: blocks are static per section.
        <Block key={i} block={block} onCite={onCite} />
      ))}
    </>
  );
}
