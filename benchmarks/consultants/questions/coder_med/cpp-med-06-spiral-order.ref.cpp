#include <iostream>
#include <vector>
int main(){
    int R, C;
    if(!(std::cin >> R >> C)) return 0;
    std::vector<int> g(R*C);
    for(int i=0;i<R*C;i++) std::cin >> g[i];
    int top=0, bot=R-1, left=0, right=C-1;
    bool first=true;
    auto emit=[&](int v){ if(!first) std::cout<<" "; std::cout<<v; first=false; };
    while(top<=bot && left<=right){
        for(int j=left;j<=right;j++) emit(g[top*C+j]);
        top++;
        for(int i=top;i<=bot;i++) emit(g[i*C+right]);
        right--;
        if(top<=bot){ for(int j=right;j>=left;j--) emit(g[bot*C+j]); bot--; }
        if(left<=right){ for(int i=bot;i>=top;i--) emit(g[i*C+left]); left++; }
    }
    std::cout << "\n";
    return 0;
}
