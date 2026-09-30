#!/usr/bin/env node
/**
 * Git pre-commit hook for Engineering Continuity Graph.
 * This hook must not fail the commit and must not block the prompt.
 */
try {
  // Credit / memory checks are informational. They never reject the commit.
} catch (_err) {
  // Swallow. A hook failure here would block the user.
}
process.exit(0);
