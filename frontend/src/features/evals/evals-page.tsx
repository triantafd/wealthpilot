import { NotWiredYet, PageHeader } from "@/components/page-shell";

/** Latest eval report against the committed baseline. Phase 8. */
export function EvalsPage() {
  return (
    <>
      <PageHeader title="Evals" description="The latest eval run against the committed baseline" />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NotWiredYet
          phase="Phase 8 · CI eval gate"
          will={[
            "Each metric against baseline.json, with the direction of change",
            "Retrieval hit@5, faithfulness, routing accuracy, SQL execution accuracy",
            "The failing cases themselves, not just the aggregate that dropped",
          ]}
          needs="the eval runner and the /evals/latest endpoint"
        />
      </div>
    </>
  );
}
