import fs from "node:fs";
import path from "node:path";
import { IncidentReplay } from "./IncidentReplay";

// Pre-rendered set used by `output: 'export'`. Reads the list of
// real incident ids the snapshot pipeline wrote to
// public/snapshot/incident_ids.json. Falls back to a single demo
// id when the file is missing (first build, before any snapshot run).
export function generateStaticParams() {
  try {
    const file = path.join(
      process.cwd(),
      "public/snapshot/incident_ids.json",
    );
    const ids: string[] = JSON.parse(fs.readFileSync(file, "utf-8"));
    if (Array.isArray(ids) && ids.length > 0) {
      return ids.map((id) => ({ id }));
    }
  } catch {
    // file missing or unreadable — fall through to default
  }
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
