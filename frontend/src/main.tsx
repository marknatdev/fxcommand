import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster } from "sonner";
import { LiveProvider } from "./api/live";
import { Layout } from "./components/Layout";
import "./index.css";
import Account from "./pages/Account";
import History from "./pages/History";
import Learning from "./pages/Learning";
import Logs from "./pages/Logs";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Risk from "./pages/Risk";
import SessionDetail from "./pages/SessionDetail";
import SessionEditor from "./pages/SessionEditor";
import Sessions from "./pages/Sessions";
import Settings from "./pages/Settings";
import Strategies from "./pages/Strategies";
import Symbols from "./pages/Symbols";

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 1000 } } });

function NotFound() {
  return <div className="py-20 text-center text-dim">Page not found</div>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <LiveProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<Overview />} />
              <Route path="sessions" element={<Sessions />} />
              <Route path="sessions/new" element={<SessionEditor />} />
              <Route path="sessions/:id" element={<SessionDetail />} />
              <Route path="sessions/:id/edit" element={<SessionEditor />} />
              <Route path="symbols" element={<Symbols />} />
              <Route path="strategies" element={<Strategies />} />
              <Route path="learning" element={<Learning />} />
              <Route path="risk" element={<Risk />} />
              <Route path="positions" element={<Positions />} />
              <Route path="history" element={<History />} />
              <Route path="account" element={<Account />} />
              <Route path="logs" element={<Logs />} />
              <Route path="settings" element={<Settings />} />
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </BrowserRouter>
        <Toaster theme="dark" position="bottom-right" richColors closeButton />
      </LiveProvider>
    </QueryClientProvider>
  </StrictMode>,
);
