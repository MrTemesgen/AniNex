# constants.py

ANILIST_API_URL = 'https://graphql.anilist.co'

GRAPHQL_QUERY = """
    fragment animeFields on Media {
      id
      idMal
      episodes
      format
      title { romaji english }
      nextAiringEpisode { episode }
    }

    query ($search: String) {
      Media (search: $search, type: ANIME, sort: [SEARCH_MATCH, START_DATE]) {
        ...animeFields
        relations {
          edges {
            relationType
            node {
              ...animeFields
              relations {
                edges {
                  relationType
                  node {
                    ...animeFields
                    relations {
                      edges {
                        relationType
                        node {
                          ...animeFields
                          relations {
                            edges {
                              relationType
                              node {
                                ...animeFields
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
"""

# Fetches a single Media's immediate relations by AniList id. Used to walk a franchise's
# prequel/sequel chain one hop at a time when the nested GRAPHQL_QUERY runs out of depth.
GRAPHQL_NODE_QUERY = """
    query ($id: Int) {
      Media (id: $id, type: ANIME) {
        id
        idMal
        episodes
        format
        title { romaji english }
        nextAiringEpisode { episode }
        relations {
          edges {
            relationType
            node {
              id
              idMal
              episodes
              format
              title { romaji english }
              nextAiringEpisode { episode }
            }
          }
        }
      }
    }
"""

MAL_API_URL = "https://api.myanimelist.net/v2"
MAL_ANIME_URL = f"{MAL_API_URL}/anime"
MAL_FORUM_URL = f"{MAL_API_URL}/forum/topics"

RELATION_TYPE_PREQUEL = 'PREQUEL'
RELATION_TYPE_SEQUEL = 'SEQUEL'

FORMAT_MOVIE = 'MOVIE'
FORMAT_OVA = 'OVA'
FORMAT_SPECIAL = 'SPECIAL'

MESSAGE_MAL_ID_NOT_FOUND = "Could not find a matching MAL ID for this season."
MESSAGE_DISCUSSION_NOT_FOUND = "Discussion thread not found on MAL. The episode may not have aired yet."
