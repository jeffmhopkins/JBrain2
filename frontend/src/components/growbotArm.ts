import { useEffect, useRef, useState } from "react";

const ARM_MS = 3_000;

/** Arm-then-confirm, as on the panels: the first tap arms for 3 s and the button disarms on
 *  its own, so a stray tap never revokes, resets or deletes anything. */
export function useArm() {
  const [armed, setArmed] = useState<Record<string, number>>({});
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const t = timers.current;
    return () => {
      for (const id of t.values()) clearTimeout(id);
    };
  }, []);
  const isArmed = (key: string) => (armed[key] ?? 0) > Date.now();
  const arm = (key: string): boolean => {
    const prev = timers.current.get(key);
    if (prev) clearTimeout(prev);
    timers.current.delete(key);
    if (isArmed(key)) {
      setArmed(({ [key]: _, ...rest }) => rest);
      return true;
    }
    setArmed((a) => ({ ...a, [key]: Date.now() + ARM_MS }));
    timers.current.set(
      key,
      setTimeout(() => {
        timers.current.delete(key);
        setArmed(({ [key]: _, ...rest }) => rest);
      }, ARM_MS),
    );
    return false;
  };
  return { isArmed, arm };
}
