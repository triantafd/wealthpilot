import { BarChart3, FileText, FlaskConical, MessageSquarePlus, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { NavLink } from "react-router-dom";

import { BackendStatus } from "@/components/backend-status";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { useThreads } from "@/features/chat/threads";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Match this path exactly rather than as a prefix. See `/chat` below. */
  exact?: boolean;
}

/**
 * The five screens in ARCHITECTURE section 7, grouped by what the user is
 * doing rather than listed flat: two are for working with the assistant, three
 * are for inspecting how it behaves.
 */
const WORK: readonly NavItem[] = [
  // "New chat", not "Chat": `/chat` with no id *is* a new conversation, which
  // was harmless while there was no list and a trap once there is one — an
  // adviser clicking what they read as "the chat screen" would leave the
  // thread they were in. The route and the page's behaviour are unchanged.
  //
  // `exact` because NavLink matches by prefix, so `/chat` is "active" on
  // `/chat/{id}` too. Invisible until the conversations list arrived, and then
  // two rows at once say "you are here".
  { to: "/chat", label: "New chat", icon: MessageSquarePlus, exact: true },
  { to: "/approvals", label: "Approvals", icon: ShieldCheck },
];

const INSPECT: readonly NavItem[] = [
  { to: "/documents", label: "Documents", icon: FileText },
  { to: "/usage", label: "Usage", icon: BarChart3 },
  { to: "/evals", label: "Evals", icon: FlaskConical },
];

function NavSection({ label, items }: { label: string; items: readonly NavItem[] }) {
  return (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map(({ to, label: itemLabel, icon: Icon, exact }) => (
            <SidebarMenuItem key={to}>
              {/* NavLink gives the active state; asChild lets the shadcn button
                  style the link itself rather than wrapping it. */}
              <NavLink to={to} end={exact}>
                {({ isActive }) => (
                  <SidebarMenuButton asChild isActive={isActive} tooltip={itemLabel}>
                    <span>
                      <Icon className="size-4" />
                      <span>{itemLabel}</span>
                    </span>
                  </SidebarMenuButton>
                )}
              </NavLink>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}

/**
 * The conversations, so one can be gone back to.
 *
 * Hidden when the sidebar is collapsed to icons, like the footer: a row here
 * is a sentence, and there is nothing useful to show of it at icon width.
 *
 * The label is the adviser's own question, truncated with CSS rather than cut
 * in JavaScript — the full text stays in the DOM, so the tooltip and a screen
 * reader get the whole thing while the row stays one line.
 */
function Conversations() {
  const { data: threads, isPending, isError } = useThreads();

  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>Conversations</SidebarGroupLabel>
      <SidebarGroupContent>
        {isPending ? (
          <div className="space-y-2 px-2 py-1">
            {[0, 1, 2].map((row) => (
              <Skeleton key={row} className="h-4 w-full" />
            ))}
          </div>
        ) : isError ? (
          <p className="px-2 py-1 text-xs text-sidebar-foreground/60">
            The conversations could not be loaded.
          </p>
        ) : threads.length === 0 ? (
          <p className="px-2 py-1 text-xs text-sidebar-foreground/60">
            No conversations yet. Ask something to start one.
          </p>
        ) : (
          <SidebarMenu>
            {threads.map((thread) => (
              <SidebarMenuItem key={thread.thread_id}>
                <NavLink to={`/chat/${thread.thread_id}`}>
                  {({ isActive }) => (
                    <SidebarMenuButton
                      asChild
                      isActive={isActive}
                      tooltip={thread.first_question}
                    >
                      <span>
                        <span className="truncate">{thread.first_question}</span>
                      </span>
                    </SidebarMenuButton>
                  )}
                </NavLink>
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        )}
      </SidebarGroupContent>
    </SidebarGroup>
  );
}

export function AppSidebar() {
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-1.5">
          <div
            aria-hidden
            className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary font-mono text-xs font-bold text-primary-foreground"
          >
            WP
          </div>
          <div className="min-w-0 group-data-[collapsible=icon]:hidden">
            <p className="truncate text-sm font-semibold">WealthPilot</p>
            <p className="truncate text-xs text-sidebar-foreground/60">Adviser assistant</p>
          </div>
        </div>
      </SidebarHeader>

      <SidebarContent>
        {/* shadcn's Sidebar renders plain divs, so the nav landmark is ours to
            add. Without it a screen reader has no way to jump to navigation. */}
        <nav aria-label="Main">
          <NavSection label="Assist" items={WORK} />
          <NavSection label="Inspect" items={INSPECT} />
        </nav>
        {/* Its own landmark rather than part of "Main": the screens are a
            fixed menu and this is a growing list of the adviser's own work,
            and a screen reader should be able to skip one to reach the other. */}
        <nav aria-label="Conversations">
          <Conversations />
        </nav>
      </SidebarContent>

      <SidebarFooter className="group-data-[collapsible=icon]:hidden">
        <BackendStatus />
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
