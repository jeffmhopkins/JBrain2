import { useEffect, useRef, useState } from "react";
import { Omnibox } from "../components/Omnibox";
import { Stream } from "../components/Stream";
import type { NotesController } from "../notes/useNotes";

const TOAST_MS = 4000;

interface HomeScreenProps {
  notes: NotesController;
  onOpenLauncher: () => void;
}

export function HomeScreen({ notes, onOpenLauncher }: HomeScreenProps) {
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function showToast(message: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(message);
    toastTimer.current = setTimeout(() => setToast(null), TOAST_MS);
  }

  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  return (
    <>
      <Stream items={notes.items} />
      <Omnibox
        onSend={(input) => void notes.send(input)}
        onConversation={() => showToast("Conversations arrive in Phase 4")}
        onOpenLauncher={onOpenLauncher}
      />
      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </>
  );
}
