import { IncidentReplay } from "./IncidentReplay";

// Pre-rendered set used by `output: 'export'` (GitHub Pages build).
// In live mode every ID still works — Next picks one of these to
// pre-render and serves the rest dynamically.
export function generateStaticParams() {
  return [{ id: "incident-demo-001" }];
}

// `dynamicParams: false` so static export only emits the IDs declared
// above. In live mode, this still flips to true via the runtime check
// in the client component (which fetches by ID from FastAPI).
export const dynamicParams = false;

export default async function IncidentReplayPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <IncidentReplay id={id} />;
}
