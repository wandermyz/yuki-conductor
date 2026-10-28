/** Harness marks, by harness name. */
import claudeCodeIcon from "../assets/harness/claude-code.svg";
import codexIcon from "../assets/harness/codex.svg";
import piIcon from "../assets/harness/pi.svg";

// Official marks: Claude's spark (claude.ai), pi's logo (pi.dev) and Codex's
// cloud (openai.com/codex).
export const HARNESS_ICONS: Record<string, string> = {
  "claude-code": claudeCodeIcon,
  pi: piIcon,
  codex: codexIcon,
};
