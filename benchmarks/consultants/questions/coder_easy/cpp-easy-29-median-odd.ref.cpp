#include <iostream>
#include <vector>
#include <algorithm>
int main() {
    long long x;
    std::vector<long long> v;
    while (std::cin >> x) v.push_back(x);
    std::sort(v.begin(), v.end());
    std::cout << v[v.size() / 2] << std::endl;
    return 0;
}
