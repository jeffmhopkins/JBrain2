// Section/citation domain dot colors for the wiki, per the chosen mock: a
// type-guided section carries a domain dot — general=steel, health(medical)=rose,
// finance=violet. This differs from the stream's DOMAIN_COLOR (general=green):
// the wiki reads general as the calm steel default, the firewalled domains as
// their warm accents. Token references only — never raw hex.

export const WIKI_DOMAIN_COLOR: Record<string, string> = {
  general: "var(--steel)",
  health: "var(--rose)",
  finance: "var(--violet)",
  location: "var(--steel)",
};

export function wikiDomainColor(domain: string): string {
  return WIKI_DOMAIN_COLOR[domain] ?? "var(--steel)";
}
