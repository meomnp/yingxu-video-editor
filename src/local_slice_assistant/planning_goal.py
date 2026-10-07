"""One structured editing goal for exported task packages and API requests."""

DEFAULT_DURATION = "90—180 秒"


def compose_planning_objective(values):
    duration = values["duration"].strip()
    if not duration:
        raise ValueError("请填写每条成片的目标时长，例如90—180秒或2分钟左右。")
    detail = ("精简版：方向比较只需两句概括，推荐一个方向；片段蓝图和交付格式不能省略。"
              if not values["mode"] else
              "详细版：至少比较两个不同切片方向，说明推荐原因；逐段列出叙事作用、衔接依据、风险和待核对画面。")
    return (f"设计 {values['count']} 条切片，每条目标时长：{duration}。"
            f"其中原声 {values['count'] - values['narration_count']} 条、带解说 {values['narration_count']} 条。"
            "每条独立作为 cuts 的一个元素，不合并成一条长视频；原声方案不写 narration，解说方案写完整 narration。"
            "设计稿要明确每条是否带解说；每句注明成片起止毫秒、完整台词和音频文件名。文件名为切片编号_句编号_简短主题.wav，如01_01_人物反击.wav；解说id为01_01。"
            "解说窗口默认静音全部原声，窗口外恢复原声；本版不分离人声，静音会丢失背景音乐，解说属实验功能，推荐专业剪辑软件精细二创。"
            "各条应以不同事件、人物视角或情绪形成实质区别，不能只换标题；素材不足以设计足够不同的方案时先说明缺口。"
            "设计稿先列方案差异表：编号、开头内容、核心事件或主题、视角、结尾落点；比较取段的重复情况，说明必要复用，不用相同主体段落仅换开头凑数。"
            "若用户提供上一批方案，也要与其比较；未提供则说明无法判断跨批雷同。差异表只放设计稿，不新增JSON执行字段。"
            f"内容方向：{values['direction'] or '根据台词推荐'}。\n{detail}\n"
            "按所选分析模板组织开头、正文和结尾，不强加其他视频类型的创作要求；引用真实文件名、起止时间和原台词，不伪造画面或改变说话人的原意。"
            "按保留区间合计预估时长；素材不足以达到目标时明确说明，不循环凑时长。"
            "台词之外的动作和静默时长需标记待看片确认。最终按任务包固定JSON协议交付源文件、源入出点和播放顺序。\n"
            f"补充要求：{values['extra'] or '无'}")
