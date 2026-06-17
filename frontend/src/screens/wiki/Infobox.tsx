// The article infobox (docs/mocks/wiki-reader-*.html): an entity-type disc OR an
// owner-added photo slot, the title, then label/value fields each carrying their
// `[n]` citations. Wiki cross-links render steel; "no article yet" links render
// the muted dotted red-link treatment. Floats right of the lead, like Wikipedia.

import type { WikiInfobox } from "../../api/client";
import { EntityTypeIcon } from "../../entities/kinds";

export function Infobox({
  infobox,
  onCite,
}: { infobox: WikiInfobox; onCite: (n: number) => void }) {
  return (
    <aside className="wiki-infobox">
      {infobox.photo ? (
        <>
          <div className="wiki-ib-photo">
            {infobox.image_url ? (
              <img className="wiki-ib-img" src={infobox.image_url} alt={infobox.title} />
            ) : (
              <EntityTypeIcon kind={infobox.kind ?? "Person"} size={46} />
            )}
          </div>
          <div className="wiki-ib-cap">owner-added photo</div>
        </>
      ) : (
        <div className="wiki-ib-disc">
          <EntityTypeIcon kind={infobox.kind ?? "Thing"} size={64} />
        </div>
      )}
      <div className="wiki-ib-title">{infobox.title}</div>
      <dl className="wiki-ib-fields">
        {infobox.fields.map((field) => (
          <div className="wiki-ib-row" key={field.label}>
            <dt>{field.label}</dt>
            <dd>
              <span
                className={field.redLink ? "wiki-redlink" : field.link ? "wiki-link" : undefined}
              >
                {field.value}
              </span>
              {field.citations.map((n) => (
                <sup key={n} className="wiki-ref">
                  <button type="button" className="wiki-cite" onClick={() => onCite(n)}>
                    [{n}]
                  </button>
                </sup>
              ))}
            </dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}
