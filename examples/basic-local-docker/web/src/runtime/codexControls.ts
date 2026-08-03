import type { CodexControl } from "../types";

function controlFromPrompt(prompt: string): CodexControl | null {
  const command = prompt.match(/^\/(plan|goal)(?:\s+([\s\S]*))?$/u);
  if (!command) return null;
  const name = command[1];
  const argument = (command[2] ?? "").trim();
  if (name === "plan") {
    return argument ? null : { action: "plan.toggle" };
  }
  if (!argument) {
    return { action: "goal.get" };
  }
  if (argument === "clear") {
    return { action: "goal.clear" };
  }
  if (argument === "pause") {
    return { action: "goal.status", status: "paused" };
  }
  if (argument === "resume") {
    return { action: "goal.status", status: "active" };
  }
  return {
    action: "goal.set",
    objective: argument.startsWith("edit ")
      ? argument.slice("edit ".length).trim()
      : argument,
  };
}

export { controlFromPrompt };
