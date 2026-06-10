export const TABS = ["capture", "chat", "search", "review", "ops"] as const;
export type Tab = (typeof TABS)[number];

const LABELS: Record<Tab, string> = {
  capture: "Capture",
  chat: "Chat",
  search: "Search",
  review: "Review",
  ops: "Ops",
};

interface BottomNavProps {
  active: Tab;
  onSelect: (tab: Tab) => void;
}

export function BottomNav({ active, onSelect }: BottomNavProps) {
  return (
    <nav className="bottom-nav" aria-label="Primary">
      {TABS.map((tab) => (
        <button
          key={tab}
          type="button"
          className={tab === active ? "nav-item active" : "nav-item"}
          aria-current={tab === active ? "page" : undefined}
          onClick={() => onSelect(tab)}
        >
          {LABELS[tab]}
        </button>
      ))}
    </nav>
  );
}
