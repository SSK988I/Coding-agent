import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, ChevronDown, ChevronRight, Copy, Sparkles } from "lucide-react";
import { MessageFiles, MessageImages } from "../../attachments/ImageAttachmentPicker";
import type { TimelineMessageItem } from "../../timeline/timelineState";

const Markdown = memo(ReactMarkdown);
const markdownPlugins = [remarkGfm];

export const TimelineMessageCard = memo(function TimelineMessageCard({
  message,
}: { message: TimelineMessageItem }) {
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const copyMessage = async () => {
    if (!message.text || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(message.text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard may be unavailable in restricted contexts.
    }
  };

  if (message.role === "user") {
    const hasAttachments = Boolean(message.images?.length || message.files?.length);
    return (
      <article className="message user">
        {hasAttachments && (
          <div className="message-user-attachments">
            {message.images?.length ? <MessageImages images={message.images} /> : null}
            {message.files?.length ? <MessageFiles files={message.files} /> : null}
          </div>
        )}
        {message.text && (
          <div className="message-body">
            <Markdown remarkPlugins={markdownPlugins}>{message.text}</Markdown>
          </div>
        )}
      </article>
    );
  }

  const isWaitingForFirstToken = message.status === "streaming" && !message.text && !message.thinking;
  const isStreamingThinking = message.status === "streaming" && !message.text && Boolean(message.thinking);
  const thinkingOnly = Boolean(message.thinking) && !message.text;

  return (
    <article className={`message assistant ${thinkingOnly ? "message--thinking-only" : ""}`.trim()}>
      <div className="message-body">
        {message.images?.length ? <MessageImages images={message.images} /> : null}
        {message.files?.length ? <MessageFiles files={message.files} /> : null}
        {message.text ? (
          <Markdown remarkPlugins={markdownPlugins}>{message.text}</Markdown>
        ) : isWaitingForFirstToken ? (
          <div className="working-indicator working-indicator--inline" data-testid="working-indicator" aria-live="polite">
            <span className="working-shimmer">Working</span>
          </div>
        ) : null}
        {message.thinking && (
          <details
            className="thinking-block"
            open={thinkingOpen}
            onToggle={(event) => setThinkingOpen(event.currentTarget.open)}
          >
            <summary className="thread-trigger thinking-block__summary">
              <span className="thread-trigger__icon" aria-hidden="true">
                <Sparkles size={14} strokeWidth={1.55} />
              </span>
              <span className={`thread-trigger__label ${isStreamingThinking ? "thinking-block__label--streaming" : ""}`.trim()}>
                {isStreamingThinking ? "正在思考…" : "思考过程"}
              </span>
              <span className="thread-trigger__chevron" aria-hidden="true">
                {thinkingOpen ? <ChevronDown size={14} strokeWidth={1.5} /> : <ChevronRight size={14} strokeWidth={1.5} />}
              </span>
            </summary>
            <div className="thread-rail thinking-block__rail">
              <pre>{message.thinking}</pre>
            </div>
          </details>
        )}
        {message.text && message.status !== "streaming" && (
          <div className="message-actions">
            <button
              type="button"
              className="message-action-btn"
              onClick={() => void copyMessage()}
              title="复制回复"
              aria-label="复制回复"
            >
              {copied ? <Check size={13} strokeWidth={1.6} /> : <Copy size={13} strokeWidth={1.6} />}
              <span>{copied ? "已复制" : "复制"}</span>
            </button>
          </div>
        )}
      </div>
    </article>
  );
});
