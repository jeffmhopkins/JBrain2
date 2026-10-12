// Whether the transcript is rendering on a public chat-share page. The share page reuses the
// owner's transcript as-is; the only thing it changes is the views whose content lives behind
// owner-only routes (generated images, chat files, map tiles, live plans), which render a quiet
// placeholder there instead of a broken image or a failing request.

import { createContext } from "react";

export const ShareModeContext = createContext(false);

/** Views a share visitor can't load — their media or live state is owner-only. */
export const SHARE_WITHHELD_VIEWS: ReadonlyMap<string, string> = new Map([
  ["generated_image", "Image not included in shared chats"],
  ["image_set", "Images not included in shared chats"],
  ["transcript", "Recording not included in shared chats"],
  ["video_analysis", "Video not included in shared chats"],
  ["location_map", "Map not included in shared chats"],
  ["hurricane_card", "Map not included in shared chats"],
  ["minecraft_map", "Map not included in shared chats"],
  ["plan_card", "Plan not included in shared chats"],
]);
