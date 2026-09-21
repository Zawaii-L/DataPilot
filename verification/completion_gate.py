"""
DataPilot v6.1

Completion Gate

任务完成判定模块

新增：
1. 文件存在检查
2. 工具失败恢复检查
3. 未恢复失败才阻断任务
"""


import os
from datetime import datetime



class CompletionGate:


    def __init__(self):

        self.check_results = []



    def add_check(
        self,
        name,
        status,
        message
    ):

        self.check_results.append(
            {
                "name": name,
                "status": status,
                "message": message
            }
        )



    def check_file_exists(
        self,
        file_path,
        name=None
    ):


        if name is None:
            name=file_path


        if os.path.exists(file_path):

            self.add_check(
                name,
                True,
                f"文件存在: {file_path}"
            )

            return True


        self.add_check(
            name,
            False,
            f"文件不存在: {file_path}"
        )

        return False




    def check_required_outputs(
        self,
        output_dir,
        required_files
    ):


        success=True


        for file in required_files:

            path=os.path.join(
                output_dir,
                file
            )


            if not self.check_file_exists(
                path,
                file
            ):

                success=False


        return success





    def check_tool_failures(
        self,
        execution_history
    ):
        """
        检查工具失败是否已经恢复

        失败:
            read_office_data

        后续成功:
            read_office_data

        认为恢复成功
        """


        if not execution_history:


            self.add_check(
                "tool_failure_check",
                True,
                "没有工具执行记录"
            )

            return True



        failed_tools={}


        success_tools=set()



        for item in execution_history:


            tool=item.get(
                "tool"
            )


            status=item.get(
                "status"
            )


            if not tool:
                continue



            if status in [
                "failed",
                "error"
            ]:

                failed_tools[tool]=True



            elif status in [
                "success",
                "completed"
            ]:

                success_tools.add(tool)




        unresolved=[]



        for tool in failed_tools:


            if tool not in success_tools:

                unresolved.append(tool)



        if unresolved:


            self.add_check(
                "tool_failure_check",
                False,
                f"存在未恢复工具失败: {unresolved}"
            )

            return False



        self.add_check(
            "tool_failure_check",
            True,
            "所有工具失败均已恢复"
        )


        return True





    def check_task_completion(
        self,
        output_dir,
        required_files,
        execution_history=None
    ):


        self.check_results=[]



        self.check_required_outputs(
            output_dir,
            required_files
        )


        self.check_tool_failures(
            execution_history
        )



        completed=all(
            item["status"]
            for item in self.check_results
        )



        return {


            "completed":completed,


            "timestamp":
                datetime.now()
                .strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),


            "checks":
                self.check_results,


            "summary":

                "任务完成"
                if completed

                else

                "任务未完成，需要继续执行"

        }