import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type SetStateAction,
} from "react";

import type { RuntimeContextValue } from "../runtime/RuntimeContext";
import type { ComposerSuggestionsContextValue } from "./Composer";

type UseComposerSuggestionsOptions = {
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  runtime: RuntimeContextValue;
};

export function useComposerSuggestions({
  input,
  setInput,
  runtime,
}: UseComposerSuggestionsOptions) {
  const [fileOptions, setFileOptions] = useState<
    Array<{ value: string; label: string; description: string }>
  >([]);
  const [selectedTriggerIndex, setSelectedTriggerIndex] = useState(0);
  const [buttonTrigger, setButtonTrigger] = useState<{
    inputPrefix: string;
  } | null>(null);
  const [dismissedTrigger, setDismissedTrigger] = useState<{
    char: string;
    start: number;
    prefix: string;
  } | null>(null);
  const dismissedTriggerRef = useRef(dismissedTrigger);

  const textTrigger = useMemo(() => {
    const match = input.match(/(^| )([$\/@])([^\s]*)$/u);
    if (!match) return null;
    const tokenLength = match[2]!.length + match[3]!.length;
    return {
      source: "text" as const,
      char: match[2]!,
      query: match[3]!,
      tokenLength,
      start: input.length - tokenLength,
    };
  }, [input]);
  const buttonQuery = buttonTrigger
    ? input.slice(buttonTrigger.inputPrefix.length)
    : "";
  const buttonTriggerIsValid = Boolean(
    buttonTrigger &&
      input.startsWith(buttonTrigger.inputPrefix) &&
      !/\s/u.test(buttonQuery),
  );
  const rawTrigger =
    buttonTriggerIsValid && buttonTrigger
      ? {
          source: "button" as const,
          char: "@",
          query: buttonQuery,
          tokenLength: buttonQuery.length,
          start: buttonTrigger.inputPrefix.length,
        }
      : textTrigger;
  const trigger =
    rawTrigger &&
    !(
      dismissedTrigger?.char === rawTrigger.char &&
      dismissedTrigger.start === rawTrigger.start &&
      dismissedTrigger.prefix === input.slice(0, rawTrigger.start)
    )
      ? rawTrigger
      : null;

  const handleInputChange = useCallback((nextInput: string) => {
    const dismissed = dismissedTriggerRef.current;
    if (
      dismissed &&
      (nextInput[dismissed.start] !== dismissed.char ||
        nextInput.slice(0, dismissed.start) !== dismissed.prefix)
    ) {
      dismissedTriggerRef.current = null;
      setDismissedTrigger(null);
    }
    setInput(nextInput);
  }, []);

  useEffect(() => {
    if (buttonTrigger && !buttonTriggerIsValid) {
      setButtonTrigger(null);
    }
  }, [buttonTrigger, buttonTriggerIsValid]);

  useEffect(() => {
    if (
      dismissedTrigger &&
      (input[dismissedTrigger.start] !== dismissedTrigger.char ||
        input.slice(0, dismissedTrigger.start) !== dismissedTrigger.prefix)
    ) {
      dismissedTriggerRef.current = null;
      setDismissedTrigger(null);
    }
  }, [dismissedTrigger, input]);

  useEffect(() => {
    if (trigger?.char !== "@") {
      setFileOptions([]);
      return;
    }
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      void runtime
        .searchFiles(trigger.query)
        .then((files) => {
          if (cancelled) return;
          setFileOptions(
            files.slice(0, 8).map((file) => ({
              value: file.path,
              label: file.file_name,
              description: file.path,
            })),
          );
        })
        .catch(() => {
          if (!cancelled) setFileOptions([]);
        });
    }, 80);
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [runtime.searchFiles, trigger]);

  const triggerOptions = useMemo(() => {
    if (!trigger) return [];
    const query = trigger.query.toLocaleLowerCase();
    if (trigger.char === "$") {
      return runtime.skills
        .filter((skill) => skill.name.toLocaleLowerCase().includes(query))
        .slice(0, 8)
        .map((skill) => ({
          value: skill.name,
          label: `$${skill.name}`,
          description:
            skill.interface?.shortDescription ||
            skill.shortDescription ||
            skill.description,
        }));
    }
    if (trigger.char === "/") {
      return [
        { value: "plan", label: "/plan", description: "Toggle Plan mode" },
        {
          value: "goal",
          label: "/goal",
          description: "Manage the thread goal",
        },
      ].filter((item) => item.value.includes(query));
    }
    return [
      ...(query
        ? []
        : [
            {
              value: "__plan_mode__",
              label: "计划模式",
              description: "先制定计划，再开始执行",
            },
            {
              value: "__goal_mode__",
              label: "目标模式",
              description: "设置持续执行的目标",
            },
          ]),
      ...fileOptions,
    ];
  }, [fileOptions, runtime.skills, trigger]);

  const selectTriggerOption = useCallback(
    (value: string) => {
      if (!trigger) return;
      if (
        trigger.char === "@" &&
        (value === "__plan_mode__" || value === "__goal_mode__")
      ) {
        if (trigger.source === "text") {
          setInput((current) =>
            current.slice(0, current.length - trigger.tokenLength),
          );
        } else {
          setInput((current) => current.slice(0, trigger.start));
        }
        setButtonTrigger(null);
        if (value === "__goal_mode__") {
          void runtime.setGoalMode(true);
        } else {
          void runtime
            .setGoalMode(false)
            .then((cleared) =>
              cleared ? runtime.setCollaborationMode("plan") : false,
            );
        }
        return;
      }
      setInput((current) => {
        const prefix = current.slice(0, current.length - trigger.tokenLength);
        const separator =
          trigger.source === "button" && prefix && !prefix.endsWith(" ")
            ? " "
            : "";
        return `${prefix}${separator}${trigger.char}${value} `;
      });
      setButtonTrigger(null);
    },
    [runtime, trigger],
  );
  const dismissTrigger = useCallback(() => {
    if (!trigger) return;
    if (trigger.source === "button") {
      setButtonTrigger(null);
      return;
    }
    const dismissed = {
      char: trigger.char,
      start: trigger.start,
      prefix: input.slice(0, trigger.start),
    };
    dismissedTriggerRef.current = dismissed;
    setDismissedTrigger(dismissed);
  }, [input, trigger]);

  const openTriggerFromButton = useCallback(() => {
    dismissedTriggerRef.current = null;
    setDismissedTrigger(null);
    setButtonTrigger({ inputPrefix: input });
    setSelectedTriggerIndex(0);
    window.requestAnimationFrame(() => {
      const textarea = document.querySelector<HTMLTextAreaElement>(
        '[data-testid="copilot-chat-input"] textarea',
      );
      if (!textarea) return;
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    });
  }, [input]);

  useEffect(() => {
    setSelectedTriggerIndex(0);
  }, [trigger?.char, trigger?.query]);

  useEffect(() => {
    setSelectedTriggerIndex((current) =>
      Math.min(current, Math.max(0, triggerOptions.length - 1)),
    );
  }, [triggerOptions.length]);

  const handleTriggerKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>): boolean => {
      if (!trigger || event.nativeEvent.isComposing) return false;
      if (event.key === "Escape") {
        event.preventDefault();
        dismissTrigger();
        return true;
      }
      if (triggerOptions.length === 0) return false;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelectedTriggerIndex(
          (current) => (current + 1) % triggerOptions.length,
        );
        return true;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelectedTriggerIndex(
          (current) =>
            (current - 1 + triggerOptions.length) % triggerOptions.length,
        );
        return true;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const selected = triggerOptions[selectedTriggerIndex];
        if (selected) selectTriggerOption(selected.value);
        return true;
      }
      return false;
    },
    [
      dismissTrigger,
      selectTriggerOption,
      selectedTriggerIndex,
      trigger,
      triggerOptions,
    ],
  );

  useEffect(() => {
    if (!trigger) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Element &&
        (target.closest("[data-efferva-trigger-menu]") ||
          target.closest('[data-testid="copilot-chat-input"]'))
      ) {
        return;
      }
      dismissTrigger();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [dismissTrigger, trigger]);

  const composerSuggestions = useMemo<ComposerSuggestionsContextValue | null>(
    () => ({
      active: Boolean(trigger),
      triggerChar: trigger?.char ?? "@",
      options: triggerOptions,
      selectedIndex: selectedTriggerIndex,
      select: selectTriggerOption,
      setSelectedIndex: setSelectedTriggerIndex,
      handleKeyDown: handleTriggerKeyDown,
      openFromButton: openTriggerFromButton,
    }),
    [
      handleTriggerKeyDown,
      openTriggerFromButton,
      selectTriggerOption,
      selectedTriggerIndex,
      trigger,
      triggerOptions,
    ],
  );

  return { composerSuggestions, handleInputChange };
}
