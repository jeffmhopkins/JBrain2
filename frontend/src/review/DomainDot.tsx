import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";

export function DomainDot({ domain }: { domain: string }) {
  return (
    <span
      className="domain-dot"
      style={{ background: DOMAIN_COLOR[domain] ?? "var(--steel)" }}
      title={DOMAIN_TITLE[domain] ?? domain}
    />
  );
}
