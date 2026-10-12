// Public chat-share URL helpers, isolated so the parse is unit-testable (main.tsx reads the
// live location; tests pass strings). The token is the path, like the report share: no
// fragment, no redeem. The shell-less ChatShareApp mounts on this path and reads the chat
// straight from the public /api/chat-share endpoint.

const SHARE_PATH = /^\/chat\/s\/([A-Za-z0-9_-]{1,128})\/?$/;

/** The public copy-link for a chat share token. */
export function chatShareUrl(token: string): string {
  return `${window.location.origin}/chat/s/${encodeURIComponent(token)}`;
}

/** The share token if `pathname` is a chat-share path (/chat/s/{token}), else null. */
export function parseChatSharePath(pathname: string = window.location.pathname): string | null {
  const match = SHARE_PATH.exec(pathname);
  return match ? (match[1] as string) : null;
}
