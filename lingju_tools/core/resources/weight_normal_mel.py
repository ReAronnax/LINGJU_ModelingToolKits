# coding: utf-8
"""Weight Normal (Smart Outside) MEL 源码（内嵌，168 行）。

「建模」面板按钮通过 mel.eval 这段文本弹出工具窗口。
更新工具：替换常量文本，或把 .mel 重新粘进来。
"""

WEIGHT_NORMAL_MEL = r'''
// ===============================
// Weight Normal - SmartOutside only (EN UI)
// Auto-show window on source/run
// ===============================

global proc float[] GetFaceNormal() {
    string $normals[] = `polyInfo -faceNormals`;
    if (!size($normals)) return {0,0,0};
    string $buffer[];
    tokenize $normals[0] $buffer;
    float $plane[3] = {0,0,0};
    if (size($buffer) >= 5) {
        $plane[0] = $buffer[2];
        $plane[1] = $buffer[3];
        $plane[2] = $buffer[4];
    }
    return $plane;
}

global proc SmartOutside(string $InAverageCtrl, string $InAngleCtrl) {
    float $myToleranceAngle = `intSliderGrp -q -value $InAngleCtrl`;
    string $buffer[];

    polySetToFaceNormal;
    string $selectedFaces[] = `ls -sl -fl`;
    if (size($selectedFaces) == 0) {
        warning "Please select at least one face before running Smart Outside.";
        return;
    }
    string $object[] = stringToStringArray($selectedFaces[0], ".");

    if (`checkBox -q -value $InAverageCtrl`) {
        polyAverageNormal -prenormalize 1 -allowZeroNormal 0 -postnormalize 0 -distance 0.0001 -replaceNormalXYZ 1 0 0;
    }

    ConvertSelectionToVertices;
    string $selectedVerts[] = `ls -sl -fl`;

    select -r $selectedFaces;
    GrowPolygonSelectionRegion;
    select -d $selectedFaces;

    ConvertSelectionToVertices;
    string $grownVerts[] = `ls -sl -fl`;

    string $myIntersector = `stringArrayIntersector`;
    stringArrayIntersector -edit -intersect $selectedVerts $myIntersector;
    stringArrayIntersector -edit -intersect $grownVerts $myIntersector;
    string $vertsToCheck[] = `stringArrayIntersector -query $myIntersector`;
    stringArrayIntersector -edit -reset $myIntersector;

    select -r $vertsToCheck;
    ConvertSelectionToFaces;
    string $intersectFaces[] = `ls -sl -fl`;

    stringArrayIntersector -edit -intersect $selectedFaces $myIntersector;
    stringArrayIntersector -edit -intersect $intersectFaces $myIntersector;
    string $facesToCheck[] = `stringArrayIntersector -query $myIntersector`;
    stringArrayIntersector -edit -reset $myIntersector;

    string $workFace;
    for ($workFace in $facesToCheck) {
        select -r $workFace;
        int $token = `tokenize $workFace "[]" $buffer`;
        string $workFaceNumber = $buffer[$token-1];

        float $faceNormal[3] = GetFaceNormal();

        ConvertSelectionToVertices;
        string $vertsOnFace[] = `ls -sl -fl`;
        string $activeVert;

        for ($activeVert in $vertsOnFace) {
            $token = `tokenize $activeVert "[]" $buffer`;
            string $activeVertNumber = $buffer[$token-1];
            int $checkThis = stringArrayContains($activeVert, $vertsToCheck);

            if ($checkThis == 1) {
                select -r $activeVert;
                ConvertSelectionToFaces;
                select -d $workFace;
                string $closeFaces[] = `ls -sl -fl`;
                string $newFace;

                for ($newFace in $closeFaces) {
                    $token = `tokenize $newFace "[]" $buffer`;
                    string $newFaceNumber = $buffer[$token-1];

                    select -r $newFace;
                    float $tempFaceNormal[] = GetFaceNormal();
                    float $angle = dotProduct($tempFaceNormal, $faceNormal, 1);

                    if ($angle > cos($myToleranceAngle * 0.017453292519943295)) {
                        select -r ($object[0] + ".vtxFace[" + $activeVertNumber + "][" + $workFaceNumber + "]");
                        float $normals[] = `polyNormalPerVertex -q -xyz`;

                        select -r ($object[0] + ".vtxFace[" + $activeVertNumber + "][" + $newFaceNumber + "]");
                        polyNormalPerVertex -xyz $normals[0] $normals[1] $normals[2];
                    }
                }
            }
        }
    }

    select -r $selectedFaces;
}

// ===============================
// UI (English, compact layout with uniform margins)
// ===============================
global proc MangesNormalToolUI() {
    if (`window -exists MangesNormalToolWindow`) deleteUI MangesNormalToolWindow;
    if (`windowPref -ex MangesNormalToolWindow`) windowPref -remove MangesNormalToolWindow;

    window -title "Weight Normal" 
           -iconName "WN" 
           -widthHeight 400 146
           -sizeable true
           MangesNormalToolWindow;

    columnLayout -adjustableColumn true -rowSpacing 4 -columnAttach "both" 8;

        // 顶部标题
        text -label "Weight Normal" -align "center" -font "boldLabelFont" -height 22;
        separator -style "in" -height 6;

        // 平均法线行
        rowLayout -numberOfColumns 2 
                  -columnWidth2 200 60
                  -columnAlign2 "left" "left"
                  -columnAttach2 "both" "both"
                  -height 22;
            text -label "Average Connected Face" -align "left";
            string $AverageConnectedFacesCheckbox = `checkBox -label "" -value false`;
        setParent ..;

        // 容差角度
        string $ToleranceAngleSlider = `intSliderGrp
            -label "Tolerance Angle"
            -field true
            -min 1 -max 180
            -fieldMinValue 1 -fieldMaxValue 180
            -value 80
            -step 1
            -columnWidth3 200 50 120
            -columnAlign3 "left" "left" "left"
        `;

        separator -style "none" -height 2;

        // 运行按钮
        button -label "Smart Outside" 
               -height 30 
               -backgroundColor 0.4 0.6 0.85
               -command ("SmartOutside " + $AverageConnectedFacesCheckbox + " " + $ToleranceAngleSlider);

        // 底部边距（与左右边距 8px 保持一致）
        separator -style "none" -height 1;

    setParent ..;

    showWindow MangesNormalToolWindow;
}

// ===============================
// Auto-launch on run
// ===============================
MangesNormalToolUI();
'''
