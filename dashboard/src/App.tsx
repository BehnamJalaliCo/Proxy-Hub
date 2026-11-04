import { QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { MotionConfig } from "motion/react";
import { Direction } from "radix-ui";
import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router";

import { AppShell } from "./components/layout/AppShell";
import { LogoMark } from "./components/layout/Brand";
import { ErrorState, Skeleton } from "./components/ui/Feedback";
import { Toaster } from "./components/ui/Toaster";
import { isApiError, setUnauthorizedHandler } from "./lib/api";
import { makeQueryClient, useMe } from "./lib/queries";

const OverviewPage = lazy(() => import("./pages/OverviewPage"));
const AccountsPage = lazy(() => import("./pages/AccountsPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const EventsPage = lazy(() => import("./pages/EventsPage"));
const SecurityPage = lazy(() => import("./pages/SecurityPage"));
const NodesPage = lazy(() => import("./pages/NodesPage"));
const TelegramPage = lazy(() => import("./pages/TelegramPage"));
const CloudflarePage = lazy(() => import("./pages/CloudflarePage"));
const ContentPage = lazy(() => import("./pages/ContentPage"));
const LoginPage = lazy(() => import("./pages/LoginPage"));
const SetupPage = lazy(() => import("./pages/SetupPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

/** Any 401 from a protected route: forget cached data and go to /login (back here afterwards). */
function UnauthorizedBridge() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  useEffect(() => {
    setUnauthorizedHandler(() => {
      const { pathname, search } = window.location;
      if (pathname.startsWith("/login") || pathname.startsWith("/setup")) return;
      qc.clear();
      const next = pathname + search;
      void navigate(next === "/" ? "/login" : `/login?next=${encodeURIComponent(next)}`, { replace: true });
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate, qc]);
  return null;
}

function Splash() {
  return (
    <div className="grid min-h-dvh place-items-center bg-bg" role="status" aria-label="در حال بارگذاری پنل">
      <div className="flex flex-col items-center gap-4">
        <LogoMark className="size-14 animate-pulse" />
        <Skeleton className="h-2 w-28 rounded-full" />
      </div>
    </div>
  );
}

function PageFallback() {
  return (
    <div className="flex flex-col gap-4" role="status" aria-label="در حال بارگذاری">
      <Skeleton className="h-9 w-48" />
      <Skeleton className="h-40 w-full rounded-3xl" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Skeleton className="h-28 rounded-3xl" />
        <Skeleton className="h-28 rounded-3xl" />
        <Skeleton className="h-28 rounded-3xl" />
        <Skeleton className="h-28 rounded-3xl" />
      </div>
    </div>
  );
}

/** Protected area: needs a live owner session (GET /api/auth/me). */
function RequireOwner() {
  const me = useMe();
  const location = useLocation();
  if (me.isPending) return <Splash />;
  if (me.isError) {
    if (isApiError(me.error) && me.error.status === 401) {
      const next = location.pathname + location.search;
      return <Navigate to={next === "/" ? "/login" : `/login?next=${encodeURIComponent(next)}`} replace />;
    }
    return (
      <div className="ph-backdrop grid min-h-dvh place-items-center px-4">
        <div className="w-full max-w-md rounded-3xl border border-line bg-surface shadow-card">
          <ErrorState error={me.error} title="پنل در دسترس نیست" onRetry={() => void me.refetch()} />
        </div>
      </div>
    );
  }
  return <AppShell />;
}

export default function App() {
  const [queryClient] = useState(makeQueryClient);
  return (
    <Direction.Provider dir="rtl">
      <MotionConfig reducedMotion="user">
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <UnauthorizedBridge />
            <Suspense fallback={<Splash />}>
              <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route path="/setup" element={<SetupPage />} />
                <Route element={<RequireOwner />}>
                  <Route
                    index
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <OverviewPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="nodes"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <NodesPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="telegram"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <TelegramPage />
                      </Suspense>
                    }
                  />
                  <Route path="content" element={<Suspense fallback={<PageFallback />}><ContentPage /></Suspense>} />
                  <Route
                    path="cloudflare"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <CloudflarePage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="accounts"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <AccountsPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="events"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <EventsPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="settings"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <SettingsPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="security"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <SecurityPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="*"
                    element={
                      <Suspense fallback={<PageFallback />}>
                        <NotFoundPage />
                      </Suspense>
                    }
                  />
                </Route>
              </Routes>
            </Suspense>
          </BrowserRouter>
          <Toaster />
        </QueryClientProvider>
      </MotionConfig>
    </Direction.Provider>
  );
}
