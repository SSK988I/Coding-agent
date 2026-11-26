import { Check, X } from "lucide-react";
import type { ModelOption } from "../../utils/format";

export function ModelPicker({
  models,
  query,
  onQueryChange,
  onSelect,
  onClose,
}: {
  models: ModelOption[];
  query: string;
  onQueryChange: (value: string) => void;
  onSelect: (model: ModelOption) => void;
  onClose: () => void;
}) {
  return (
    <section className="model-picker" aria-label="选择模型">
      <div className="model-picker-header">
        <div>
          <strong>选择模型</strong>
          <span>仅显示已配置凭据的 Provider</span>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭模型选择器">
          <X size={16} strokeWidth={1.6} />
        </button>
      </div>
      <div className="model-picker-search">
        <input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="筛选模型或 Provider…"
          aria-label="筛选模型"
        />
      </div>
      <div className="model-list">
        {models.map((model) => (
          <button
            type="button"
            key={`${model.provider}:${model.id}`}
            className={model.current ? "active" : ""}
            onClick={() => onSelect(model)}
          >
            <span>
              <strong>{model.name}</strong>
              <small>{model.id}{model.input?.includes("image") ? " · 支持图片" : ""}</small>
            </span>
            <em>
              {model.provider}
              {model.current && <Check size={13} strokeWidth={1.8} aria-hidden="true" />}
              {model.current ? " 当前" : ""}
            </em>
          </button>
        ))}
        {!models.length && <p>没有匹配的可用模型。</p>}
      </div>
    </section>
  );
}
