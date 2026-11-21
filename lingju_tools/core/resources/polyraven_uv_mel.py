# coding: utf-8
"""PolyravenUV v1.0.5 MEL 源码（内嵌）。

只包含 TransferUV / Rectangulate 用到的 24 个 proc（1345 行），
不含原文件的 UI、布局、重叠检测、朝向等无关过程，也不 source 外部 .mel。
入口：prUV_TransferUV / prUV_GetSelected / prUV_RectangulateShell。
更新工具：替换常量文本即可。
"""

POLYRAVEN_UV_MEL = r'''
/*
Copyright (c) 2019 Szilárd Szlákó <info@polyraven.com>, All rights reserved.

SOFTWARE: Polyraven UV tools v1.0.5

AUTHOR: Szilárd Szlákó (polyraven.com), and Giuseppe Seda

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

THIS IS PRORIETARY SOFTWARE. IF YOU USE THE PERSONAL LICENSE 
YOU CANNOT DOWNLOAD OR INSTALL THE SOFTWARE FOR OTHER USERS;
EACH USER MUST INDIVIDUALLY ACQUIRE A PERSONAL LICENSE AND
DOWNLOAD AND INSTALL THE SOFTWARE.
IF YOU WOULD LIKE TO DOWNLOAD OR INSTALL THE SOFTWARE FOR
OTHER USERS IN YOUR COMPANY OR ORGANIZATION, YOU NEED 
TO ACQUIRE A STUDIO LICENSE.

A PERSONAL LICENSE OR STUDIO LICENSE CAN BE ACQUIRED
AT GUMROAD.COM/POLYRAVEN

The above copyright notice and this permission notice shall be included
 in all copies or substantial portions of the Software.
*/


global proc float[] prUV_GetBB_FromUVs(string $Input[])
{	

	float $bbTMP[];
	//string $cUVs=`polyListComponentConversion -ff -tuv $Input`;
	float $CoordList[]=`polyEditUV -q $Input`;

	$bbTMP[0]=$CoordList[0];
	$bbTMP[1]=$CoordList[0];
	$bbTMP[2]=$CoordList[1];
	$bbTMP[3]=$CoordList[1];
	$bbTMP[4]=size($CoordList);//UVCount

	int $cN;
	for($cN=2;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[0]>$CoordList[$cN])$bbTMP[0]=$CoordList[$cN];
		if($bbTMP[1]<$CoordList[$cN])$bbTMP[1]=$CoordList[$cN];
	}
	for($cN=3;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[2]>$CoordList[$cN])$bbTMP[2]=$CoordList[$cN];
		if($bbTMP[3]<$CoordList[$cN])$bbTMP[3]=$CoordList[$cN];
	}
	return $bbTMP;
}

global proc float[] prUV_GetBB_FromUV(string $Input)
{	

	float $bbTMP[];
	float $CoordList[]=`polyEditUV -q $Input`;

	$bbTMP[0]=$CoordList[0];
	$bbTMP[1]=$CoordList[0];
	$bbTMP[2]=$CoordList[1];
	$bbTMP[3]=$CoordList[1];

	int $cN;
	for($cN=2;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[0]>$CoordList[$cN])$bbTMP[0]=$CoordList[$cN];
		if($bbTMP[1]<$CoordList[$cN])$bbTMP[1]=$CoordList[$cN];
	}
	for($cN=3;$cN<size($CoordList);$cN+=2)
	{
		if($bbTMP[2]>$CoordList[$cN])$bbTMP[2]=$CoordList[$cN];
		if($bbTMP[3]<$CoordList[$cN])$bbTMP[3]=$CoordList[$cN];
	}
	return $bbTMP;
}

global proc int prUV_StrPos(string $String, string $Character)
{
   int $i=0;
   
   for($i=size($String);$i>0;$i--)
   {
       
       string $SubStr = substring($String,$i,$i);
       
       if( $SubStr == $Character){return $i;}
   }

   return -1;
}

global proc string prUV_getunderfirstindex(string $InputString)
{
    	
	string $Result = $InputString;
	
    int $idA=prUV_StrPos($InputString,":");
	if($idA>-1)
	{
		int $idB=prUV_StrPos($InputString,"[");
		
		if($idB>-1 && $idB<$idA)
		{
			$Result = substring($InputString,1,$idB) + substring($InputString,$idB+1,$idA-1) + "]";
		}
	}

	return $Result;	
}

global proc prUV_TransferUV(string $referenceobject, int $type)
{

	string  $Objects[];
	int $i;
	int $n;

	$Objects = `ls -os -o -l` ;
	$n=size($Objects);
	
	if($n==0){error  "No Objects Selected";}
	else
	{
		if( `objExists $referenceobject` ) 
		{
		    select -add $referenceobject;
			DeleteHistory;
			select -r $Objects;
		
			//Initialize default progressBar of maya
			global string $gMainProgressBar;
			progressBar -edit   -beginProgress
			-isInterruptable true  -status "UV Transfer..."
			-maxValue $n	-minValue 0    $gMainProgressBar;


			for($i=0;$i<$n;++$i)
			{
				if(`progressBar -query -isCancelled $gMainProgressBar`) break;	
				
				if($type>=0)
				{
					transferAttributes -transferPositions 0 -transferNormals 0 -transferUVs 2 -transferColors 0 -sampleSpace $type -sourceUvSpace "map1" -targetUvSpace "map1" -searchMethod 3 -flipUVs 0 -colorBorders 0 $referenceobject $Objects[$i];
				}
				else
				{
					transferAttributes -transferPositions 1 -transferNormals 0 -transferUVs 0 -transferColors 0 -sampleSpace 3 -sourceUvSpace "map1" -targetUvSpace "map1" -searchMethod 3 -flipUVs 0 -colorBorders 0 $referenceobject $Objects[$i];
				}

				progressBar -edit   -step 1 $gMainProgressBar;	
			}

			//free progresBar from this process
			progressBar -edit   -endProgress $gMainProgressBar;	
			DeleteHistory;
		}
		else
		{
			error "Reference object does not exists!";
		}
	}
}

global proc string prUV_GetSelected()
{
	string  $Objects[];
	int $n;
	$Objects = `ls -os -o` ;
	$n=size($Objects);
	
	if($n==0){error  "Select the object first";}
	else
	{
		if($n>1){warning  "More than one object is selected, using the last one as reference.";}
		
		return $Objects[$n-1];
		
	}
	
	return "(reference object not set)";
}

global proc string[] prUV_StringArray_shellList_2017()
{
	string $sel[] = `ls -sl`;

	ConvertSelectionToUVs;
	ConvertSelectionToUVShell;
	
	int $Cn=1;
	
	string $list[];
	int $n=0;
	
	int $PCn=size(`ls -sl`);
	
	if($PCn!=0)
	{
		int $SelUV[]=`polyEvaluate -uvc`;
		$PCn=$SelUV[0];
	}

	if($PCn!=0)
	{
		//Initialize default progressBar of maya
			global string $gMainProgressBar;
			progressBar -edit
			-beginProgress
			-isInterruptable true
			-status "List UV Shells..."
			-maxValue $PCn
			-minValue 0
			$gMainProgressBar;
		
		while($Cn!=0)
		{
			string $sUVs[]=`ls -sl`;
			$Cn=`size($sUVs)`;
			int $ShellSize=0;

			if($Cn!=0)
			{
				string $first=prUV_getunderfirstindex($sUVs[0]);
				select -r $first;
				
				/*$list[$n]=$first;
				$n++;*/
				
				ConvertSelectionToUVShell;
				
				$list[$n]=stringArrayToString(`ls -sl`," ");
				$n++;

				int $SelUV[]=`polyEvaluate -uvc`;
				$Cn=$SelUV[0];

				select -tgl $sUVs;
								
				if(`progressBar -query -isCancelled $gMainProgressBar`){ break;}

			}

			progressBar -edit -step $Cn $gMainProgressBar;
			
			$PCn=$Cn;
			
		}
	progressBar -edit   -endProgress $gMainProgressBar;
	}
	
	int $s = size($list);
	//print ("\n shells: " + $s);
	
	select $sel;
	
	return $list;	
}

global proc string[] prUV_StringArray_shellList_2018()
{

	global int $MainProgressBarCancelled;
	//print " Cancelled F:";
	//print $MainProgressBarCancelled;

	string $emptyarray[];
	string $uvShellList[];

	$prUV_GlobalShellList=$emptyarray;

    ConvertSelectionToUVs;
    int $i,$j=0,$k,$cnt=0,$unique,$cancel=0;
    string $objects[]=`ls -sl -o `;

    string $uvShellListTmp[];
    string $parentsTmp[]=`listRelatives -f -p $objects`;
    string $parents[];

    for($i=0; $i<size($parentsTmp);$i++)
    {
        
        $k=0;
        $unique=1;
        while((size($parents)!=0) && (($k<size($parents)) && ($parents[$k]!=$parentsTmp[$i]))) 
        {
            $k++;
        }

        if($k!=size($parents))
            $unique=0;

        if($unique==1){
            $parents[$j]=$parentsTmp[$i];
            $j++;
        }
    }

    global string $gMainProgressBar;
    progressBar -edit
    -beginProgress
    -isInterruptable true
    -status "List UV Shells(2018)..."
    -maxValue (size($parents)*100)
    -minValue 0
    $gMainProgressBar;

    int $shellId[];
    string $objectCorr[];
    int $activeShellsIds[] = `polyEvaluate  -aus`;
    int $numShells = size($activeShellsIds);
    int $activeShellsIdsTmp[];
    int $numShellsTmp=0; 

    $j=0;
    $cnt=0;
    for($i=0; $i<size($parents) && !$MainProgressBarCancelled ;$i++)
    {

		/*if(`progressBar -query -isCancelled $gMainProgressBar`)
    	{ 
            $cancel=1;
            $MainProgressBarCancelled=1;
            print "\n Canceled At: F";
            break;
   		}*/

		/*if(`progressBar -query -isCancelled $gMainProgressBar`)
		{
			$MainProgressBarCancelled=1;
			print "\n Canceled At: F";
		}*/

    	int $PrevProgress=0;
        $activeShellsIdsTmp=`polyEvaluate -aus $parents[$i]`;
        $numShellsTmp = $numShellsTmp + size($activeShellsIdsTmp);

        $k=0;
      	for($j=0; $j<size($activeShellsIdsTmp); $j++)
        {

            $uvShellList[$cnt]=stringArrayToString(`polyEvaluate -uis $activeShellsIdsTmp[$k] $parents[$i]`," ");
            $cnt++;
            $k++;
       
    		int $cProgress=floor((float($k)/float(size($activeShellsIdsTmp)))*100.0);
			int $cStep=($cProgress-$PrevProgress);
			progressBar -edit -step  $cStep $gMainProgressBar;
			$PrevProgress=$cProgress;

        }
    }

    progressBar -edit   -endProgress $gMainProgressBar;
   
	return $uvShellList;
}

global proc string[] prUV_StringArray_shellList()
{
	global int $MayaVersion;
	if($MayaVersion<=2017)
	{
		return prUV_StringArray_shellList_2017();
	}
	else
	{
		return prUV_StringArray_shellList_2018();
	}

	string $emptylist[];
	return $emptylist;
}

global proc string[] prUV_GprUV_shellList2018(string $input)
{
	string $emptyarray[];
	global string $prUV_GlobalShellList[];
	string $uvShellList[];

	if($input=="")
	{

		$prUV_GlobalShellList=$emptyarray;

	    ConvertSelectionToUVs;
	    int $i,$j=0,$k,$cnt=0,$unique,$cancel=0;
	    string $objects[]=`ls -sl -o `;

	    string $uvShellListTmp[];
	    string $parentsTmp[]=`listRelatives -f -p $objects`;
	    string $parents[];
	   

	    //Initialize default progressBar of maya
	   /* global string $gMainProgressBar;
	    progressBar -edit
	    -beginProgress
	    -isInterruptable true
	    -status "List UV Shells(2018)..."
	    -maxValue (size($parentsTmp)*2)
	    -minValue 0
	    $gMainProgressBar;*/

	    for($i=0; $i<size($parentsTmp);$i++)
	    {
	        
	        $k=0;
	        $unique=1;
	        /*progressBar -edit -step 1 $gMainProgressBar;
	        if(`progressBar -query -isCancelled $gMainProgressBar`)
	        { 
	            $cancel=1;
	            /* print "getting canceled\n";*/ 
	         //   break;
	       // }
	        while((size($parents)!=0) && (($k<size($parents)) && ($parents[$k]!=$parentsTmp[$i]))) 
	        {
	            $k++;
	        }

	        if($k!=size($parents))
	            $unique=0;

	        if($unique==1){
	            $parents[$j]=$parentsTmp[$i];
	            $j++;
	        }
	    }

	  //  if ($cancel==0)
	   // {   

	    	//progressBar -edit -maxValue (size($parents)*2) $gMainProgressBar;
	    	//progressBar -edit -progress  (size($parents)) $gMainProgressBar;

		    global string $gMainProgressBar;
		    progressBar -edit
		    -beginProgress
		    -isInterruptable true
		    -status "List UV Shells(2018)..."
		    -maxValue (size($parents)*100)
		    -minValue 0
		    $gMainProgressBar;

	        int $shellId[];
	        string $objectCorr[];
	        int $activeShellsIds[] = `polyEvaluate  -aus`;
	        int $numShells = size($activeShellsIds);
	        int $activeShellsIdsTmp[];
	        int $numShellsTmp=0; 

	        $j=0;
	        $cnt=0;
	        for($i=0; $i<size($parents);$i++)
	        {

	        	 if(`progressBar -query -isCancelled $gMainProgressBar`)
			        { 
			            $cancel=1;
			            break;
			       }

	        	int $PrevProgress=0;
	            $activeShellsIdsTmp=`polyEvaluate -aus $parents[$i]`;
	            $numShellsTmp = $numShellsTmp + size($activeShellsIdsTmp);

	            $k=0;
	           	//for($j=($numShellsTmp - size($activeShellsIdsTmp)); $j<$numShellsTmp; $j++)
	          	for($j=0; $j<size($activeShellsIdsTmp); $j++)
	            {
	                $uvShellListTmp=`polyEvaluate  -uis $activeShellsIdsTmp[$k] $parents[$i]`;

	                appendStringArray($prUV_GlobalShellList,$uvShellListTmp,size($uvShellListTmp));
	                $prUV_GlobalShellList[size($prUV_GlobalShellList)]="";

	                $uvShellList[$cnt]=$uvShellListTmp[0];
	                $cnt++;
	                $k++;
	           
            		int $cProgress=floor((float($k)/float(size($activeShellsIdsTmp)))*100.0);
					int $cStep=($cProgress-$PrevProgress);
					progressBar -edit -step  $cStep $gMainProgressBar;
					$PrevProgress=$cProgress;

	            }
	           // progressBar -edit -step 1 $gMainProgressBar;
	        }

            progressBar -edit   -endProgress $gMainProgressBar;

	  //  }

	
	    
	    
	}
	else
	{
		int $gn=0;
		int $found=0;
		for($gn=0;$gn<size($prUV_GlobalShellList) && $found==0;$gn++)
		{
			if($prUV_GlobalShellList[$gn]==$input)
			{
				int $count=0;

				while($prUV_GlobalShellList[$gn+$count]!="")
				{
					$uvShellList[size($uvShellList)]=$prUV_GlobalShellList[$gn+$count];
					$count++;
				}
				$found=1;
			}
		}  
	}



	return $uvShellList;
}

global proc string[] prUV_ShellList2017()
{
	string $sel[] = `ls -sl`;

	ConvertSelectionToUVs;
	ConvertSelectionToUVShell;
	
	int $Cn=1;
	
	string $list[];
	int $n=0;
	
	int $PCn=size(`ls -sl`);
	
	if($PCn!=0)
	{
		int $SelUV[]=`polyEvaluate -uvc`;
		$PCn=$SelUV[0];
	}

	if($PCn!=0)
	{
		//Initialize default progressBar of maya
			global string $gMainProgressBar;
			progressBar -edit
			-beginProgress
			-isInterruptable true
			-status "List UV Shells..."
			-maxValue $PCn
			-minValue 0
			$gMainProgressBar;
		
		while($Cn!=0)
		{
			string $sUVs[]=`ls -sl`;
			$Cn=`size($sUVs)`;
			int $ShellSize=0;

			if($Cn!=0)
			{
				string $first=prUV_getunderfirstindex($sUVs[0]);
				select -r $first;
				
				$list[$n]=$first;
				$n++;
				
				ConvertSelectionToUVShell;
				
				int $SelUV[]=`polyEvaluate -uvc`;
				$Cn=$SelUV[0];

				select -tgl $sUVs;
								
				if(`progressBar -query -isCancelled $gMainProgressBar`){ break;}

			}

			progressBar -edit -step $Cn $gMainProgressBar;
			
			$PCn=$Cn;
			
		}
	progressBar -edit   -endProgress $gMainProgressBar;
	}
	
	int $s = size($list);
	print ("\n shells: " + $s);
	
	select $sel;
	
	return $list;	
}

global proc string[] prUV_ShellList()
{
	global int $MayaVersion;
	if($MayaVersion<=2017)
	{
		return prUV_ShellList2017();
	}
	else
	{
		return prUV_GprUV_shellList2018("");
	}

	string $emptylist[];
	return $emptylist;
}

global proc prUV_StraightenEdge_Scale(string $UVList[])
{
	if(size($UVList)==0)
	{
		ConvertSelectionToUVs;
		$UVList=`ls -sl -fl`;
	}

	float $coordsdata[] = `polyEvaluate -bc2`;
	float $W = $coordsdata[1]-$coordsdata[0];
	float $H = $coordsdata[3]-$coordsdata[2];
	float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
	float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;

	int $n=0;int $Pinned=0;
	for($n=0;$n<size($UVList) && $Pinned==0;$n++)
    {
        float $v[]=`polyPinUV -q -v $UVList[$n]`;
        if($v[0]!=0)
        {
        	$Pinned=1;
            float $c[]=`polyEditUV -q $UVList[$n]`;
            $pivotU=$c[0];
            $pivotV=$c[1];
            print "\n\n\n PINNED !!! \n\n\n";
        }
    }


	if($H>$W){polyEditUV -pu $pivotU -pv $pivotV -su 0.00001 -sv 1 $UVList;}
	else{polyEditUV -pu $pivotU -pv $pivotV -su 1 -sv 0.00001 $UVList;}

	performPolyPinSelectionUVOptions 0;

}

global proc string[] prUV_GetEndUVs()
{

    ConvertSelectionToContainedEdges;
    string $oSel[]=`ls -sl`; 
    string $E[]=`ls -sl -fl`;   
    ConvertSelectionToUVs;
    string $V[]=`ls -sl -fl`;   

    int $vS=size($V);

    int $vN=0;

    string $sV[];
    int $sVn=0;

    for($vN=0;$vN<$vS;$vN++)
    {
        select $V[$vN];
        ConvertSelectionToEdges;
        string $TmpE[]=`ls -sl -fl`;
        int $EdgeNA=size($TmpE);
        select -d $E;
        $TmpE=`ls -sl -fl`;
        int $EdgeNB=size($TmpE);

        if(($EdgeNA-$EdgeNB)==1)
        {
           $sV[$sVn]=$V[$vN];
           $sVn++;   
        }  
    }

    select -r $V;
    select -d $sV;
    string $mV[]=`ls -sl -fl`;

    select -r $sV;
    return $sV;
}

global proc string[] prUV_GetOrderedUVList(string $allUVS[],string $edges[],string $endUV[])
{
    string $el[];

    select -r $allUVS;
    GrowPolygonSelectionRegion;
    select -d $allUVS;
    string $Invalid[]=`ls -sl`;

    select -r $edges;
    GrowPolygonSelectionRegion;
    select -d $edges;
    string $InvalidEdges[]=`ls -sl`;


    select -r $endUV[0];

    appendStringArray($el,`ls -sl`,size(`ls -sl`));

    while(size($el)<size($allUVS))
    {
        select -r $el[size($el)-1];
        ConvertSelectionToEdges;
        select -d $InvalidEdges;
        ConvertSelectionToUVs;
        select -d $Invalid;
        select -d $el;
        appendStringArray($el,`ls -sl`,size(`ls -sl`));
    }

    return $el;
}

global proc float[] prUV_GetEdgeLoopPivot(string $UVList[])
{

    float $PI=3.1415926535897932384626433832795;

    select -r $UVList[0];
    select -add $UVList[size($UVList)-1];

    float $coordsdata[]=`polyEvaluate -bc2`;

    float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
    float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;

    return {$pivotU,$pivotV}; 
}

global proc float prUV_EdgeLoopAngle(string $UVList[])
{

    float $PI=3.1415926535897932384626433832795;

    float $fC[]=`polyEditUV -q $UVList[0]`;

    float $sC[]=`polyEditUV -q $UVList[size($UVList)-1]`;

    float $dU=$sC[0]-$fC[0]; float $dV=$sC[1]-$fC[1];

    print ("\n dU:" + $dU + " dV:" + $dV);

    float $Angle=(atan2($dV,$dU)*180/$PI);

    return $Angle;
}

global proc float prUV_Get3DDist(string $f,string $s)
{

    float $fC[]=`xform -q -ws -t $f`;
    float $sC[]=`xform -q -ws -t $s`;
    float $D[]={$fC[0]-$sC[0],$fC[2]-$sC[2],$fC[3]-$sC[3]};
    float $dist = sqrt(($D[0]*$D[0])+($D[1]*$D[1])+($D[2]*$D[2]));
    return $dist;
}

global proc float prUV_EdgeDistRatio(string $UVList[])
{
    float $oDist;
    float $Dist3D;

    int $n;
    string $fUV=$UVList[0];
    float $fC[]=`polyEditUV -q $fUV`;
    float $pC[]=$fC;
    for($n=1;$n<size($UVList);$n++)
    {
        string $sUV=$UVList[$n];

        float $sC[]=`polyEditUV -q $sUV`;

        float $dU=$sC[0]-$fC[0];
        float $dV=$sC[1]-$fC[1];

        $oDist += sqrt(($dV*$dV)+($dU*$dU));
        $Dist3D += prUV_Get3DDist($fUV,$sUV);

        $fUV=$sUV;
        $fC=$sC;
    }

    return ($oDist/$Dist3D);
}

global proc int prUV_StraightenEdge(string $UVList[],int $ParallelToAxes)
{ 
    int $n;
    int $PinedID=0;int $Pinned=0;int $PinnedN=0;
    for($n=0;$n<size($UVList) && $PinnedN<2;$n++)
    {
        float $v[]=`polyPinUV -q -v $UVList[$n]`;
        if($v[0]!=0)
        {
            if($PinnedN==0)
            {
                $PinedID=$n;
                $Pinned=1;
            }

            $PinnedN++;   
        }
    }

    float $tAngle=prUV_EdgeLoopAngle($UVList);
    float $dRatio=prUV_EdgeDistRatio($UVList);

    float $PI=3.1415926535897932384626433832795;

    float $cosAngle=cos($tAngle*($PI/180));
    float $sinAngle=sin($tAngle*($PI/180));

    if($ParallelToAxes)
    {
        print "\n $ParallelToAxes";
        if(abs($cosAngle)>=0.5)
        {
            $sinAngle=0;
            if($cosAngle>0)$cosAngle=1;
            if($cosAngle<0)$cosAngle=-1;
        }
        else
        {
            $cosAngle=0;
            if($sinAngle>0)$sinAngle=1;
            if($sinAngle<0)$sinAngle=-1;
        }
    }
 
    
    if($PinnedN>=1&&$ParallelToAxes)
    {
        prUV_StraightenEdge_Scale($UVList);
    }
    else
    {
        string $fUV=$UVList[$PinedID];
        float $fC[]=`polyEditUV -q $fUV`;
        float $pC[]=$fC;
        for($n=$PinedID+1;$n<size($UVList);$n++)
        {
            string $sUV=$UVList[$n];

            float $sC[]=`polyEditUV -q $sUV`;

            float $dU=$sC[0]-$fC[0];
            float $dV=$sC[1]-$fC[1];
            float $Dist = sqrt(($dV*$dV)+($dU*$dU));

            //Distance based on 3D
            /*if($Pinned==1)
            {
                float $Dist3D= prUV_Get3DDist($fUV,$sUV);
                $Dist = $Dist3D*$dRatio;
            }*/

            float $nU=($pC[0]+$Dist*$cosAngle);
            float $nV=($pC[1]+$Dist*$sinAngle);
            $pC[0]=$nU; $pC[1]=$nV;

            polyEditUV -u ($nU-$sC[0]) -v ($nV-$sC[1]) $sUV;

            $fUV=$sUV;
            $fC=$sC;
        }

        if($Pinned==1)
        {
            $fUV=$UVList[$PinedID];
            $fC=`polyEditUV -q $fUV`;
            $pC=$fC;
            $cosAngle*=-1;
            $sinAngle*=-1;
            for($n=$PinedID-1;$n>=0;$n--)
            {
                string $sUV=$UVList[$n];

                float $sC[]=`polyEditUV -q $sUV`;

                float $dU=$sC[0]-$fC[0];
                float $dV=$sC[1]-$fC[1];
                float $Dist = sqrt(($dV*$dV)+($dU*$dU));

                //Distance based on 3D
                /*
                if($Pinned==1)
                {
                    float $Dist3D= prUV_Get3DDist($fUV,$sUV);
                    $Dist = $Dist3D*$dRatio;
                }
                */

                float $nU=($pC[0]+$Dist*$cosAngle);
                float $nV=($pC[1]+$Dist*$sinAngle);
                $pC[0]=$nU; $pC[1]=$nV;

                polyEditUV -u ($nU-$sC[0]) -v ($nV-$sC[1]) $sUV;

                $fUV=$sUV;
                $fC=$sC;
            }
        }
    }
    return $Pinned;       
}

global proc prUV_StraightenSelectedEdge(int $ParallelToAxes)
{
    string $alledges[]=`ls -sl -fl`;
    ConvertSelectionToUVs;
    string $oUV[]=`ls -sl`;
    string $shells[]=prUV_ShellList();

    int $n=0;
    for($n=0;$n<size($shells);$n++)
    {
    	select -r $shells[$n];
    	ConvertSelectionToUVShell;ConvertSelectionToEdges; string $s[]=`ls -sl`;
    	select -r $alledges;select -d $s; string $n[] = `ls -sl`;
    	select -r $alledges;select -d $n; string $edges[]=`ls -sl -fl`;

	    if(size($edges)>0)
	    {
	        ConvertSelectionToUVs;
	        string $AllUV[]=`ls -sl -fl`;
	        string $endUV[]=prUV_GetEndUVs();
	        string $UVList[];

	        if(size($endUV)==2)$UVList=prUV_GetOrderedUVList($AllUV,$edges,$endUV);

	        if(size($UVList)!=0)
	        {
	            float $tPivot[]=prUV_GetEdgeLoopPivot($UVList);
	            if(prUV_StraightenEdge($UVList,$ParallelToAxes)==0)
	            {
	                float $cPivot[]=prUV_GetEdgeLoopPivot($UVList);
	                polyEditUV -u ($tPivot[0]-$cPivot[0]) -v ($tPivot[1]-$cPivot[1]) $UVList;
	            }

	            select -r $UVList;
	            performPolyPinSelectionUVOptions 0;

	        }
	    }
	}

	select -r $oUV;
}

global proc string[] prUV_GetUVShellEdgeBorderFromBorderUV(string $BorderUV[])
{

	string $pBorderEdge[]=`polyListComponentConversion -fuv -te  $BorderUV`;
	
	$pBorderEdge=`ls -fl $pBorderEdge`;
	
	string $borderEdges[];
	int $n;
	for($n=0;$n<size($pBorderEdge);$n++)
	{
		int $fine=0;
		
		/*select -r $pBorderEdge[$n];
		ConvertSelectionToUVs;
		int $uvSize[]=`polyEvaluate -uvc`;
		int $sizeUV=$uvSize[0];*/

		//string $uvs[]=`polyListComponentConversion -fe -tuv  $pBorderEdge[$n]`;
		//$uvs=`ls -fl $uvs`;
		//int $sizeUV=size($uvs);
		
		string $uvs[]=`polyEvaluate -uep $pBorderEdge[$n]`;
		$uvs=stringToStringArray($uvs[0], " ");
		$uvs=stringArrayRemoveDuplicates($uvs);
		int $sizeUV=size($uvs);
		

		if($sizeUV==4 || $sizeUV==3)$fine=1;
		else
		{

			/*select -r $pBorderEdge[$n];
			ConvertSelectionToFaces;
			int $uvSize[]=`polyEvaluate -fc`;
			int $sizeFaces=$uvSize[0];*/

			/*string $faces[]=`polyListComponentConversion -fe -tf  $pBorderEdge[$n]`;
			$faces=`ls -fl $faces`;
			int $sizeFaces=size($faces)-1;*/

			string $faces[]=`polyInfo -ef $pBorderEdge[$n]`;
			$faces=stringToStringArray($faces[0],":");
			$faces=stringToStringArray($faces[1]," ");
			int $sizeFaces=size($faces)-1;

			if($sizeUV==2 && $sizeFaces==1)$fine=1;
		}

		//if(size($uvs)==4 && size($faces)==2)$fine=1;
		//if(size($uvs)==2 && size($faces)==1)$fine=1;
		//if(size($uvs)==3 && size($faces)==2)$fine=1;
		//if(size($uvs)==2 && size($faces)==2)$fine=0;
		
		if($fine)
		{
			$borderEdges[size($borderEdges)]=$pBorderEdge[$n];
		}

	}

	return $borderEdges;
}

global proc prUV_RectangulateOneShell(string $InputUVShell,string $OriginalEdges[])
{

	string $uvShell[]=stringToStringArray($InputUVShell," ");
	select -r $uvShell;
	ConvertSelectionToUVShellBorder;
	string $uvBorder[]=`ls -sl`;
	select -tgl $uvShell;
	string $uvInShell[]=`ls -sl -fl`;
	
	string $edgeBorder[];
	global int $MayaVersion;
	if($MayaVersion<=2017)
	{
		select -r $uvShell;
		ConvertSelectionToEdges;
		polySelectBorderShell 1;
		$edgeBorder=`ls -sl`;
	}
	else
	{
		$edgeBorder=prUV_GetUVShellEdgeBorderFromBorderUV($uvBorder);
	}
	
	select -r $uvShell;
	ConvertSelectionToVertices;
	ConvertSelectionToContainedEdges;
	select -deselect $edgeBorder;
	
	string $EdgeInShell[]=`ls -sl -fl`;
	
	
	if(size($OriginalEdges)!=0)
	{

		string $tmpList[]=stringArrayRemove($EdgeInShell,$OriginalEdges);
		$tmpList=stringArrayRemove($tmpList,$OriginalEdges);
		if(size($tmpList)!=0)
		{
			select -r $tmpList[0];
			polySelectEdges edgeUVLoopOrBorder;
			prUV_StraightenSelectedEdge(1);
		    ConvertSelectionToUVShell;
		    u3dUnfold -ite 1 -p 0 -bi 1 -tf 1 -ms 1024 -rs 2;
		    u3dOptimize -ite 1 -pow 1 -sa 1 -bi 1 -tf 1 -ms 1024 -rs 2;
		    UVEditorUnpinAll;
		    select -r $EdgeInShell;
		}
	}


	string $TopBorder[];
	string $BottomBorder[];
	int $tN=0; int $bN=0;
	
	string $LeftBorder[];
	string $RightBorder[];
	int $lN=0; int $rN=0;
		
	if(`size($EdgeInShell)`!=0)
	{
		
		int $SelEdge[]=`polyEvaluate -ec`;

		//Initialize default progressBar of maya
		global string $gMainProgressBar;
		progressBar -edit
		-beginProgress
		-isInterruptable true
		-status "Rectangulate Shell..."
		-maxValue $SelEdge[0]
		-minValue 0
		$gMainProgressBar;
		
		while($SelEdge[0]!=0)
		{
		
			if(`progressBar -query -isCancelled $gMainProgressBar`) break;
		
			string $edge = prUV_getunderfirstindex($EdgeInShell[0]);
			
			select -r $edge;
			polySelectEdges edgeUVLoopOrBorder;
			string $edgeloop[]=`ls -sl`;
			//int $EdgeLoopN[]=`polyEvaluate -ec`;
			//ConvertSelectionToUVs;
			string $EdgeUVs[]=`polyListComponentConversion -tuv $edgeloop`;
			

			//float $coordsdata[] = `polyEvaluate -bc2`;
			float $coordsdata[]=prUV_GetBB_FromUVs($EdgeUVs);
			float $W = $coordsdata[1]-$coordsdata[0];
			float $H = $coordsdata[3]-$coordsdata[2];
			float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
			float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;
		
			if($H>$W){polyEditUV -pu $pivotU -pv $pivotV -su 0.00001 -sv 1 $EdgeUVs;}
			else{polyEditUV -pu $pivotU -pv $pivotV -su 1 -sv 0.00001 $EdgeUVs;}
		
			

			//select -deselect $uvInShell;
			//string $uvs[]=`ls -sl -fl`;
			
			//select -r $uvs[0];
			//float $coordA[] = `polyEvaluate -bc2`;
			//select -r $uvs[1];
			//float $coordB[] = `polyEvaluate -bc2`;
			
			$EdgeUVs=`ls -fl $EdgeUVs`;
			string $uvs[]=stringArrayRemove($uvInShell,$EdgeUVs);
			print "\n $uvs: \n";
			print $uvs;
			print "\n\n";
			float $coordA[] = prUV_GetBB_FromUV($uvs[0]);
			float $coordB[] = prUV_GetBB_FromUV($uvs[1]);

			string $top;
			string $bottom;
			string $left;
			string $right;
			
			if($H>$W)
			{
				if($coordA[2]>$coordB[2])
				{
					$top=$uvs[0];
					$bottom=$uvs[1];
				}
				else
				{
					$top=$uvs[1];
					$bottom=$uvs[0];
				}

				$TopBorder[$tN]=$top;
				$BottomBorder[$bN]=$bottom;
				$tN += 1; $bN += 1;
			}
			else
			{
				if($coordA[0]>$coordB[0])
				{
					$right=$uvs[0];
					$left=$uvs[1];
				}
				else
				{
					$right=$uvs[1];
					$left=$uvs[0];
				}	
				
				$LeftBorder[$lN]=$left;
				$RightBorder[$rN]=$right;
				$lN += 1; $rN += 1;
				
			}
			
			
			//select -r $EdgeInShell;
			//select -deselect $edgeloop;
			//$EdgeInShell=`ls -sl`;
				
			$edgeloop=`ls -fl $edgeloop`;
			$EdgeInShell=stringArrayRemove($edgeloop,$EdgeInShell);


			if(`size($EdgeInShell)`!=0){$SelEdge=`polyEvaluate -ec`;}
			else {$SelEdge[0]=0;}
			
			progressBar -edit -step (size($edgeloop)) $gMainProgressBar;
			
		}
		
		progressBar -edit   -endProgress $gMainProgressBar;
		
	}
	
	int $plN=$lN; int $prN=$rN; int $ptN=$tN; int $pbN=$bN;

	{
		select -r $uvBorder;
		select -d $TopBorder;
		select -d $BottomBorder;
		select -d $LeftBorder;
		select -d $RightBorder;
		
		string $uv[]=`ls -sl -fl`;
		
		//float $coordsdata[] = `polyEvaluate -bc2`;
		float $coordsdata[] = prUV_GetBB_FromUVs($uv);
		float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
		float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;
		
		//for($n=0;$n<4;$n++)
		for($n=0;$n<size($uv);$n++)
		{
			//select -r $uv[$n];
			//float $coordsdata[] = `polyEvaluate -bc2`;
			float $coord[] = prUV_GetBB_FromUV($uv[$n]);
		
			if($coord[0]<$pivotU)
			{
				if($plN==0)
				{
					$LeftBorder[$lN]=$uv[$n];
					$lN += 1;
				}
			}
			else
			{
				if($prN==0)
				{
				$RightBorder[$rN]=$uv[$n];
				$rN += 1;
				}
			}
			
			if($coord[2]>$pivotV)
			{
				if($ptN==0)
				{
				$TopBorder[$tN]=$uv[$n];
				$tN += 1;
				}
			}
			else
			{
				if($pbN==0)
				{
				$BottomBorder[$bN]=$uv[$n];
				$bN += 1;
				}
			}

		}
	}



	for($n=0;$n<4;$n++)
	{
		
		int $pn=1;
		if($n==0){select -r $TopBorder; $pn=$ptN;}
		if($n==1){select -r $BottomBorder; $pn=$pbN;}
		if($n==2){select -r $LeftBorder; $pn=$plN;}
		if($n==3){select -r $RightBorder; $pn=$prN;}
	
	
		if($pn!=0)
		{
			string $oUV[]=`ls -sl`;
			GrowPolygonSelectionRegion;
			string $gUV[]=`ls -sl`;
			select -r $oUV;
			
			ConvertSelectionToEdges;
			ConvertSelectionToUVs;
			string $cUV[]=`ls -sl`;
			
			select -d $gUV;
			select -tgl $cUV;
			
			select -d $uvInShell;
				
			if($n!=0 && $ptN!=0){select -d $TopBorder;}
			if($n!=1 && $pbN!=0){select -d $BottomBorder;}
			if($n!=2 && $plN!=0){select -d $LeftBorder;}	
			if($n!=3 && $prN!=0){select -d $RightBorder;}
			
			string $tmp[]=`ls -sl`;
			select -d $uvShell;
			select -tgl $tmp;

		}
	
		//float $coordsdata[] = `polyEvaluate -bc2`;
		float $coordsdata[] = prUV_GetBB_FromUVs(`ls -sl`);
		float $pivotU= ($coordsdata[1]+$coordsdata[0])/2.0;
		float $pivotV= ($coordsdata[3]+$coordsdata[2])/2.0;
	
		if($n==0||$n==1){polyEditUV -pu $pivotU -pv $pivotV -su 1 -sv 0.00001 ;}
		if($n==2||$n==3){polyEditUV -pu $pivotU -pv $pivotV -su 0.00001 -sv 1 ;}
	}
}

global proc prUV_RectangulateShell()
{
	string $oSel[]=`ls -sl -fl`;

	string $OriginalEdges[];
	int $en[]=`polyEvaluate -ec`;
	if($en[0]!=0)
	{	
		ConvertSelectionToEdges;
		$OriginalEdges=`ls -sl -fl`;
		/*polySelectEdges edgeUVLoopOrBorder;
		prUV_StraightenSelectedEdge(1);
	    ConvertSelectionToUVShell;
	    u3dUnfold -ite 1 -p 0 -bi 1 -tf 1 -ms 1024 -rs 2;
	    u3dOptimize -ite 100 -pow 1 -sa 1 -bi 1 -tf 1 -ms 1024 -rs 2;
	    UVEditorUnpinAll;
	    select -r $oSel;*/
	}

	ConvertSelectionToUVs;
	string $Shells[]=prUV_StringArray_shellList();
	int $ShellN=`size($Shells)`;
	int $ShellListN=0;
	
	for($ShellListN=0;$ShellListN<$ShellN;$ShellListN++)
	{
		prUV_RectangulateOneShell($Shells[$ShellListN],$OriginalEdges);
	}
	select -r $oSel;
}
'''
