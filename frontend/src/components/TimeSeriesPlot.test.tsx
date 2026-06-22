import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { type PlotSeries, TimeSeriesPlot } from "./TimeSeriesPlot";

function series(over: Partial<PlotSeries> = {}): PlotSeries {
  return {
    label: "CPU",
    color: "var(--steel)",
    values: [1, 2, 3],
    fmt: (v) => v.toFixed(1),
    ...over,
  };
}

describe("TimeSeriesPlot", () => {
  it("renders one sparkline per series with current + peak/low readouts", () => {
    const { getByText } = render(<TimeSeriesPlot series={[series({ values: [1, 4, 2] })]} />);
    expect(getByText("CPU")).toBeInTheDocument();
    expect(getByText("2.0")).toBeInTheDocument(); // current = last value
    expect(getByText("4.0 peak")).toBeInTheDocument();
    expect(getByText("1.0 low")).toBeInTheDocument();
  });

  it("omits a series whose values are all null, and renders nothing when all empty", () => {
    const { container, queryByText } = render(
      <TimeSeriesPlot
        series={[
          series({ label: "GPU", values: [null, null] }),
          series({ label: "Fan", values: [3] }),
        ]}
      />,
    );
    expect(queryByText("GPU")).toBeNull(); // all-null series dropped
    expect(queryByText("Fan")).not.toBeNull();

    const { container: empty } = render(
      <TimeSeriesPlot series={[series({ values: [null, null] })]} />,
    );
    expect(empty.querySelector(".plot-stack")).toBeNull();
    expect(container.querySelector(".plot-stack")).not.toBeNull();
  });

  it("breaks the line at null gaps rather than dropping to zero", () => {
    const { container } = render(<TimeSeriesPlot series={[series({ values: [1, null, 3] })]} />);
    const d = container.querySelector("path")?.getAttribute("d") ?? "";
    // Two move commands: the gap starts a new sub-path instead of a line-to.
    expect((d.match(/M/g) ?? []).length).toBe(2);
  });
});
