import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { type PlotSeries, TimeSeriesPlot } from "./TimeSeriesPlot";

function series(over: Partial<PlotSeries> = {}): PlotSeries {
  return {
    label: "CPU",
    lines: [{ color: "var(--steel)", values: [1, 2, 3] }],
    fmt: (v) => v.toFixed(1),
    ...over,
  };
}

describe("TimeSeriesPlot", () => {
  it("renders one sparkline per series with current + peak/low readouts", () => {
    const { getByText } = render(
      <TimeSeriesPlot
        series={[series({ lines: [{ color: "var(--steel)", values: [1, 4, 2] }] })]}
      />,
    );
    expect(getByText("CPU")).toBeInTheDocument();
    expect(getByText("2.0")).toBeInTheDocument(); // current = last value
    expect(getByText("4.0 peak")).toBeInTheDocument();
    expect(getByText("1.0 low")).toBeInTheDocument();
  });

  it("omits a series whose values are all null, and renders nothing when all empty", () => {
    const { container, queryByText } = render(
      <TimeSeriesPlot
        series={[
          series({ label: "GPU", lines: [{ color: "var(--steel)", values: [null, null] }] }),
          series({ label: "Fan", lines: [{ color: "var(--steel)", values: [3] }] }),
        ]}
      />,
    );
    expect(queryByText("GPU")).toBeNull(); // all-null series dropped
    expect(queryByText("Fan")).not.toBeNull();

    const { container: empty } = render(
      <TimeSeriesPlot
        series={[series({ lines: [{ color: "var(--steel)", values: [null, null] }] })]}
      />,
    );
    expect(empty.querySelector(".plot-stack")).toBeNull();
    expect(container.querySelector(".plot-stack")).not.toBeNull();
  });

  it("breaks the line at null gaps rather than dropping to zero", () => {
    const { container } = render(
      <TimeSeriesPlot
        series={[series({ lines: [{ color: "var(--steel)", values: [1, null, 3] }] })]}
      />,
    );
    // The stroked line is the last path; a gap starts a new sub-path (two M cmds).
    const paths = container.querySelectorAll("path");
    const d = paths[paths.length - 1]?.getAttribute("d") ?? "";
    expect((d.match(/M/g) ?? []).length).toBe(2);
  });

  it("draws a filled peak band under the line when a band is given", () => {
    const { container } = render(
      <TimeSeriesPlot
        series={[
          series({ lines: [{ color: "var(--steel)", values: [1, 2, 3], band: [2, 3, 5] }] }),
        ]}
      />,
    );
    // A band adds a filled (fill-opacity) path in addition to the stroked line.
    const filled = [...container.querySelectorAll("path")].filter(
      (p) => p.getAttribute("fill-opacity") != null,
    );
    expect(filled.length).toBe(1);
    // Peak/low span the band too, so the true peak (5) is the axis max.
    expect(container.textContent).toContain("5.0 peak");
  });

  it("renders a colored legend swatch per line for a multi-line panel", () => {
    const { container, getByText } = render(
      <TimeSeriesPlot
        series={[
          series({
            label: "Network",
            lines: [
              { label: "down", color: "var(--periwinkle)", values: [1, 2] },
              { label: "up", color: "var(--orchid)", values: [3, 4] },
            ],
          }),
        ]}
      />,
    );
    expect(getByText("down")).toBeInTheDocument();
    expect(getByText("up")).toBeInTheDocument();
    expect(container.querySelectorAll(".plot-swatch").length).toBe(2);
  });
});
