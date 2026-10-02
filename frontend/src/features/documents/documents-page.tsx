import { NotWiredYet, PageHeader } from "@/components/page-shell";

/** Indexed corpus and re-index control. Phase 1 ingestion, Phase 7 UI. */
export function DocumentsPage() {
  return (
    <>
      <PageHeader title="Documents" description="The indexed policy corpus the agent cites" />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NotWiredYet
          phase="Phase 1 · RAG baseline"
          will={[
            "The ten synthetic policy documents, with chunk counts per document",
            "A re-index button, which only re-embeds files whose sha256 changed",
            "Which documents a given answer actually cited",
          ]}
          needs="the ingestion pipeline and the /documents endpoint"
        />
      </div>
    </>
  );
}
