import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ClipboardList } from "lucide-react";
import type { PlanRevisionPayload } from "../../../shared/types";

const Markdown = memo(ReactMarkdown);
const markdownPlugins = [remarkGfm];

export const TimelinePlanCard = memo(function TimelinePlanCard({
  plan,
  badge,
}: { plan: PlanRevisionPayload; badge: string }) {
  return (
    <section className="plan-card" data-testid="plan-card">
      <div className="plan-card-header">
        <span className="thread-trigger__icon" aria-hidden="true">
          <ClipboardList size={15} strokeWidth={1.55} />
        </span>
        <span className="plan-badge">{badge}</span>
        <strong>{plan.title}</strong>
        <span className="plan-card-meta">revision {plan.revision} · {plan.digest.slice(0, 12)}</span>
      </div>
      <div className="plan-markdown">
        <Markdown remarkPlugins={markdownPlugins}>{plan.markdown}</Markdown>
      </div>
    </section>
  );
});
