import { Component, type ReactNode } from "react";

import { ApiError } from "../../lib/api";
import { ErrorState } from "./Feedback";

const RENDER_FAILED = new ApiError(
  0,
  "render_failed",
  "شاید پنل همین حالا به‌روز شده است یا داده‌ی نامنتظره‌ای رسید. صفحه را دوباره بارگذاری کنید.",
);

interface Props {
  children: ReactNode;
  /** The root variant: a card on the backdrop in place of the whole panel. */
  fullScreen?: boolean;
}

interface State {
  error: unknown;
}

/** Catches a render error or a page chunk that failed to load, so only this part shows a Persian
 * card (with a reload) instead of React unmounting the whole panel. Nothing is logged: errors may
 * carry what the owner typed (rule 3). */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error ?? RENDER_FAILED };
  }

  override render(): ReactNode {
    if (this.state.error === null) return this.props.children;
    const card = <ErrorState error={RENDER_FAILED} title="این بخش نمایش داده نشد" onRetry={() => window.location.reload()} />;
    if (!this.props.fullScreen) return card;
    return (
      <div className="ph-backdrop grid min-h-dvh place-items-center px-4">
        <div className="w-full max-w-md rounded-3xl border border-line bg-surface shadow-card">{card}</div>
      </div>
    );
  }
}
