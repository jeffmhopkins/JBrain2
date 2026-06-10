import { useEffect, useRef } from "react";
import { attachmentUrl } from "../api/client";
import { groupByDay, relativeTime } from "../notes/grouping";
import { DOMAIN_COLOR, DOMAIN_LABEL } from "../notes/modes";
import type { StreamItem } from "../notes/useNotes";
import { ClipIcon } from "./icons";

function headText(item: StreamItem): string {
  const time = item.pending
    ? `${relativeTime(item.createdAt)} · pending`
    : relativeTime(item.createdAt);
  const domainLabel = DOMAIN_LABEL[item.domain];
  if (domainLabel && item.destination) return `${time} · ${domainLabel} → ${item.destination}`;
  if (domainLabel) return `${time} · ${domainLabel}`;
  return time;
}

function NoteRow({ item }: { item: StreamItem }) {
  return (
    <div className="note">
      <div className="note-head">
        <span
          className="domain-dot"
          style={{ background: DOMAIN_COLOR[item.domain] ?? "var(--steel)" }}
        />
        {headText(item)}
      </div>
      <div className="note-body">{item.body}</div>
      {(item.attachments.length > 0 || item.pending) && (
        <div className="note-chips">
          {item.attachments.map((att) =>
            att.id ? (
              <a
                key={`${item.key}-${att.id}`}
                className="chip"
                href={attachmentUrl(att.id)}
                target="_blank"
                rel="noreferrer"
              >
                <ClipIcon size={12} /> {att.filename}
              </a>
            ) : (
              <span key={`${item.key}-${att.filename}`} className="chip">
                <ClipIcon size={12} /> {att.filename}
              </span>
            ),
          )}
          {item.pending && <span className="chip chip-pending">pending sync</span>}
        </div>
      )}
    </div>
  );
}

export function Stream({ items }: { items: StreamItem[] }) {
  const scrollerRef = useRef<HTMLElement>(null);

  // New rows land at the bottom; keep the latest in view like a chat log.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per append; the effect reads the DOM, not the items.
  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length]);

  const groups = groupByDay(items, (item) => item.createdAt);

  return (
    <main className="stream" ref={scrollerRef}>
      <div className="stream-inner">
        {items.length === 0 && (
          <p className="stream-empty">Nothing captured yet — write your first entry below.</p>
        )}
        {groups.map((group) => (
          <section key={group.key}>
            <h2 className="day-header">{group.label}</h2>
            <div className="day-card">
              {group.items.map((item) => (
                <NoteRow key={item.key} item={item} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}
