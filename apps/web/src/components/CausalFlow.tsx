"use client";

import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  type Edge,
  type Node,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

type Cause = {
  cause_kind: string;
  confidence: number;
  delta_seconds: number;
  derivation: string;
  strategy: string;
  cause_props: Record<string, unknown>;
};

export function CausalFlow({
  incidentId,
  incidentLabel,
  causes,
}: {
  incidentId: string;
  incidentLabel: string;
  causes: Cause[];
}) {
  const { nodes, edges } = useMemo(() => {
    const incidentNode: Node = {
      id: "incident",
      data: { label: `🚨 ${incidentLabel}` },
      position: { x: 380, y: 200 },
      style: {
        background: "#3a1414",
        color: "#ffd1d1",
        border: "1px solid #ff6b6b",
        borderRadius: 8,
        padding: 10,
        fontWeight: 600,
        minWidth: 220,
        textAlign: "center" as const,
      },
    };

    const causeNodes: Node[] = causes.map((c, i) => {
      const total = causes.length;
      const angle = ((i + 1) / (total + 1)) * Math.PI;
      const x = 380 - Math.cos(angle) * 320;
      const y = 200 - Math.sin(angle) * 180;
      const conf = c.confidence ?? 0;
      const tone =
        conf >= 0.7
          ? { bg: "#0f2a1e", border: "#3ed598", color: "#bff7da" }
          : conf >= 0.4
          ? { bg: "#2a1f0a", border: "#ffb950", color: "#ffe2b5" }
          : { bg: "#1d2235", border: "#525c6f", color: "#cdd3e0" };
      const props = c.cause_props ?? {};
      const headline =
        (props.deployment_id as string) ||
        (props.metric as string) ||
        (props.commit_sha as string)?.slice(0, 7) ||
        c.cause_kind;
      return {
        id: `cause-${i}`,
        data: {
          label: `${c.cause_kind}\n${headline}\nconf ${(conf * 100).toFixed(0)}%`,
        },
        position: { x, y },
        style: {
          background: tone.bg,
          color: tone.color,
          border: `1px solid ${tone.border}`,
          borderRadius: 8,
          padding: 10,
          minWidth: 180,
          whiteSpace: "pre-line" as const,
          fontSize: 11,
        },
      };
    });

    const causeEdges: Edge[] = causes.map((c, i) => ({
      id: `e-${i}`,
      source: `cause-${i}`,
      target: "incident",
      animated: true,
      label: `${c.strategy}\nΔ ${(c.delta_seconds / 60).toFixed(1)}m`,
      labelStyle: { fill: "#a9b1bd", fontSize: 10 },
      labelBgStyle: { fill: "#0c1019" },
      style: { stroke: "#525c6f" },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#525c6f" },
    }));

    return {
      nodes: [incidentNode, ...causeNodes],
      edges: causeEdges,
    };
  }, [causes, incidentLabel]);

  return (
    <div className="h-[420px] rounded-lg border border-ink-700 overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable
        nodesConnectable={false}
        edgesFocusable={false}
        zoomOnScroll={false}
      >
        <Background color="#272e3d" gap={20} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
