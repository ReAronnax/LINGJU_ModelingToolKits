## 使用说明： 

1. 将 Lingju_Modeling_Tools_v104.pyd  文件拷贝到   **...\Documents\maya\20xx\scripts**   (...是在你的文档所在的文件夹。例如：D:\Users\Lingju_admin\Documents\maya\20xx\scripts\)  文件夹下​如果使用的是中文版，则拷贝到 **...\Documents\maya\20xx\zh_CN\scripts**
   
2. 将  “零距数码maya常用建模工具.mel”  拖拽进maya即可打开，或将  “零距数码maya常用建模工具.mel”  用记事本打开将里面的内容中键拖拽到工具架。 

>[!NOTE]
>### v104 功能更新： 
> 
>- BUG修复 
>
>- 增加 - 平均线段功能 
>
>- 增加 - 选中元素加换选 
>
>- UI更新 
>
>### v103 功能更新：
>
>- ***initial release*** 

## 功能说明：

​	集成了一些常用工具，具体的工具类型分布有些不合理，多提意见，多使用。多找bug 
 
​		
### **建模** 

**平均线段**: ————————————— 

>- `冻结，居中，重置： 和MAYA自带功能一致` 

>- `置底： 一键将pivot放到物体的中心底部` 

>- `物体至世界中心： 将物件一键移动到时间中心，并且是物件的底部在Y=0` 

**平均线段**: ————————————— 

>- `平均： 自动按当前选择线段方向平均所有线段`
	 
>- `弧线： 自动按当前选择线段两端的平滑弧线为基准平均线段`

>- `打直平均： 自动按当前选择线段两段打直并平均所有线段`

**智能合并/分离**: ————————————— 

>- `智能合并： 物件合并时不产生历史，并将pivot移动到中心`

>- `智能分离： 自动判断分离面，还是分离物体，分离时不产生多余组，多余历史，`

>- `分离时保留选中面： 当需要复制选中面时，选择 “是”`
 
​		
>- ​`转角改线： 能自动将转角的口字布线，改为Y字布线。（如不能更改，先清理物件历史）`

>- `选中元素加换选： 按照选中元素（点线面）的位置增加环线`

>- `智能插入I： 功能强大的插入面工具，能自动调节带弧形边缘的角度，插入以后是基本等宽的 `

>- `智能插入II： 简单快捷的插入面工具，能插入单面的片，使用offset可以等宽调节 `

>- `拖拽吸附： 点击后左键不放，拖拽选中物体，到另外一个物体表面，按住Ctrl是按5°旋转，按住Shift是复制，Ctrl+Shift是对齐到最近的线。`

>- `沿最近边对齐： 移动物体到需要对齐的边，运行自动和最近的的线对齐。`
 
​		
### **选择** 

**按角度选择面**：————————————— 

>- `角度： 按照 “选择” 键选择的面，判断角度为超过的周围的所有面。非常方便。不用考虑布线。移动滑块能实时增加或者减少选择的面`
  
**选择相似物体**：————————————— 

>- `选择相似物体： 一键选择场景中相似的物体。不管有没有重置变换。`
 
​		
>- `忽略大小： 如果选择 “否”： 这需要保证所有物体实际大小完全一致，才会选择。如果选择 “是”：则不伦实际大小有没有变化都能选中。`
  
>- `隔段选择： 自动判断两条Loop或者Ring中间隔的线段数，并按Loop或者Ring循环选择下去`

>- `选区选择： 能按照当前选择的线段组成的闭合区间，分组选择，非常方面。直观预选择，需要开启Preference - Selection - Preselection Highlight`

>- `选硬边： 自动选择当前物体的所有硬边`



