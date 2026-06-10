interface PlaceholderScreenProps {
  title: string;
  phase: string;
  blurb: string;
}

export function PlaceholderScreen({ title, phase, blurb }: PlaceholderScreenProps) {
  return (
    <section className="placeholder">
      <h2>{title}</h2>
      <p className="phase-badge">{phase}</p>
      <p>{blurb}</p>
    </section>
  );
}
