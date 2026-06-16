import { useCallback, useEffect, useRef, useState } from "react";

const DISARM_MS = 3000;

/** Armed tap-again for destructive controls: first tap arms, a second within
 * 3s confirms; a timeout or any other control disarms. One instance is shared
 * across a detail's controls (proposals, inference reject, reopen) so arming
 * one disarms the rest. */
export function useArmed(): [string | null, (key: string) => boolean] {
  const [armed, setArmed] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  const tap = useCallback(
    (key: string): boolean => {
      if (timer.current !== null) clearTimeout(timer.current);
      if (armed === key) {
        setArmed(null);
        return true;
      }
      setArmed(key);
      timer.current = setTimeout(() => setArmed(null), DISARM_MS);
      return false;
    },
    [armed],
  );
  return [armed, tap];
}
