"""Refs classificadas manualmente para as sub-abas de Estoque."""

# Câmara fria = produtos acabados (cremes/açaís/gelatos/potes/sachês em estoque)
CAMARA_FRIA_REFS = {
    1315, 1317, 1360, 1359, 1311, 903, 1379, 1380, 1355, 1300, 519, 553, 1322,
    536, 791, 1351, 571, 1381, 1382, 1354, 701, 708, 769, 1378, 605, 1455,
    1356, 746, 627, 925, 861, 528, 1348, 806, 1353, 1349, 1352, 826, 699, 849,
    835, 834, 833, 836, 813, 1342, 1308, 1449, 1323, 1320, 1358, 1357, 1324,
    1459, 1305, 1331, 1347, 182, 183, 1325, 810, 255, 588, 1306, 270, 740,
    344, 348, 347, 346, 345, 789, 790, 1304, 1219, 1335, 244, 1340, 1341,
    1337, 1336, 1361, 1362, 1363, 1364, 1376, 1377, 1369, 1371, 1373, 1374,
    761, 1326, 1346, 130, 1329,
}

# Polpas AQUI (guardadas fisicamente na fábrica)
POLPAS_AQUI_REFS = {924, 805, 851, 742, 568, 933, 778}

# Polpas em TERCEIROS (Lockfrio) — armazém à parte
POLPAS_LOCKFRIO_REFS = {923, 909, 932, 934, 879}

# Reprocesso = usuário lançará diariamente; deixado em branco
REPROCESSO_REFS = set()
