"""
DataPilot v6.1-8
Professional Chart Executor
"""

from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


class ChartExecutor:

    def __init__(self, output_dir="outputs"):

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)


    def execute(self, dataframe, chart_plan):

        chart_type = chart_plan["type"]

        x = chart_plan.get("x")
        y = chart_plan.get("y")

        plt.figure(figsize=(10,5))


        if chart_type == "line":

            plt.plot(
                dataframe[x],
                dataframe[y]
            )

        elif chart_type == "bar":

            plt.bar(
                dataframe[x],
                dataframe[y]
            )


        plt.title(
            chart_plan.get(
                "title",
                y
            )
        )

        plt.xlabel(
            chart_plan.get(
                "x_label",
                x
            )
        )

        plt.ylabel(
            chart_plan.get(
                "y_label",
                y
            )
        )


        if "datetime" in str(dataframe[x].dtype):

            plt.gcf().autofmt_xdate()

            ax = plt.gca()
            ax.xaxis.set_major_locator(
                mdates.AutoDateLocator()
            )


        filename = (
            f"{y}_{chart_type}.png"
        )

        path = self.output_dir / filename

        plt.tight_layout()

        plt.savefig(
            path,
            dpi=300
        )

        plt.close()

        return str(path)



def execute_chart(dataframe, chart_plan, output_dir="outputs"):

    executor = ChartExecutor(output_dir)

    return executor.execute(
        dataframe,
        chart_plan
    )
