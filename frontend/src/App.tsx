import { Navigate, Route, Routes } from "react-router-dom";

import { AppSidebar } from "@/components/app-sidebar";
import { PageHeader } from "@/components/page-shell";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { ApprovalsPage } from "@/features/approvals/approvals-page";
import { ChatPage } from "@/features/chat/chat-page";
import { DocumentsPage } from "@/features/documents/documents-page";
import { EvalsPage } from "@/features/evals/evals-page";
import { UsagePage } from "@/features/usage/usage-page";

function NotFound() {
  return (
    <>
      <PageHeader title="Not found" />
      <div className="mx-auto w-full max-w-xl px-4 py-12">
        <p className="text-sm text-muted-foreground">
          That route does not exist. Pick a screen from the sidebar.
        </p>
      </div>
    </>
  );
}

export function App() {
  return (
    <SidebarProvider>
      <AppSidebar />
      {/* SidebarInset is the main column; it shrinks when the sidebar expands. */}
      <SidebarInset className="flex min-h-svh flex-col">
        <Routes>
          {/* Chat is the app's purpose, so "/" lands there rather than on a
              dashboard nobody asked for. `replace` keeps "/" out of history. */}
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/evals" element={<EvalsPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </SidebarInset>
    </SidebarProvider>
  );
}
